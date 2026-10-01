#!/usr/bin/env python3
"""T39: the shipped image installs exactly what the lock pins, and does it the same way twice.

`make freedom` grades a dependency *closure* - which licences are in scope. It cannot answer the
question a customer asks: **which versions are in the artifact?** Before this, the answer was
"whatever PyPI called newest when the layer was built", which is neither reproducible nor auditable.

Four claims, each with its own witness and each with a way to fake it:

1. **Two cold builds agree.** `docker build --no-cache` twice, from the same context, and compare
   `pip freeze --all` as text. `--no-cache` is what makes this a measurement rather than a tautology:
   with layer reuse the second build would return the first one's bytes and prove nothing.
2. **The image contains nothing the lock does not pin.** Every `name==version` line of the freeze
   (minus `pip`, `setuptools`, `wheel` and this project, which come from the base tag) must be in
   `docker/requirements-lock.txt` at exactly that version.
3. **The lock reaches the image.** Proved by *not* assuming it: build with a doctored pin and show
   the doctoring in the freeze. A constraint pip ignored would leave the newest version there.
4. **The licence gate runs on the shipping platform.** One pin - `greenlet` - is required by
   SQLAlchemy's `platform_machine` marker on linux and by nothing on darwin arm64, so a host on that
   platform cannot see it at all. `FC-1` therefore also runs *inside the image* over `core,vision`.
   On a host that *is* a linux platform the pin is visible there too, so the anti-theatre witness
   changes to the one that is true everywhere: the image run grades the artifact's 13-root closure,
   not this host's 22-root development one. A hole you have measured is a known limitation; a hole you
   have not looked for is a bug.

    usage: ./.venv/bin/python scripts/lock_e2e.py [options]

      --mutate MODE       produce a deliberately wrong state and prove the gate notices:
                          `bump-pin` (a copy of the lock with one pin changed to a valid other
                          version) and `new-dep` (a copy of pyproject.toml declaring a package no
                          pin covers) are judged by `dependency-lock`'s own exit code;
                          `doctored-pin` (build with a wrong pin) and `no-lock` (build with an
                          empty constraints file: the pre-T39 world) are judged by the freeze
      --expect-fail       accepted for symmetry with the other e2e scripts; with `--mutate` the
                          contract is already "exit 0 iff the mutation was caught"
      --keep              leave the built tags in place for debugging
  """

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from synthverify.compliance.dependency_lock import (  # noqa: E402
    LOCK_PATH,
    NOT_A_DEPENDENCY,
    TARGET_PLATFORM_PINS,
    read_lock,
)

DOCKERFILE = REPO / "docker" / "Dockerfile"
MUTATION_LOCK = REPO / "docker" / ".lock-mutation.txt"

#: A pin this script perturbs. It must be a version pip can actually install, or the mutation
#: would test the resolver's error handling instead of the gate's.
SAMPLE_PACKAGE = "anyio"
SAMPLE_DOCTORED = "4.15.1"
#: Declared by nothing and pinned by nothing, but installed by every environment that has run
#: the test suite - so `new-dep` exercises "a pyproject edit adds a package the lock has never
#: heard of" whether or not the driver happens to be importable here.
SAMPLE_NEW_DEP = "pg8000>=1.31"

CHECKS: list[str] = []
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    (CHECKS if ok else FAILURES).append(f"{label} -> {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""))


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


# --------------------------------------------------------------------------- building


def build(tag: str, lock: str | None = None) -> tuple[int, str]:
    """One cold build: `--no-cache` so pip re-resolves and the layer is really rebuilt."""
    cmd = ["docker", "build", "--no-cache", "-f", str(DOCKERFILE), "-t", tag, "."]
    if lock:
        cmd.insert(2, f"--build-arg=LOCK={lock}")
    print(f"building {tag}{' from ' + lock if lock else ''} (--no-cache)")
    result = run(cmd)
    if result.returncode:
        tail = "\n".join(result.stdout.splitlines()[-15:] + result.stderr.splitlines()[-15:])
        print(tail)
    return result.returncode, result.stdout + result.stderr


@contextlib.contextmanager
def doctored_lock(text: str):
    """Write a constraints file inside the build context, and guarantee it is not left there."""
    MUTATION_LOCK.write_text(text)
    try:
        yield str(MUTATION_LOCK.relative_to(REPO))
    finally:
        MUTATION_LOCK.unlink(missing_ok=True)


def image_freeze(tag: str) -> tuple[int, str]:
    result = run(["docker", "run", "--rm", "--entrypoint", "python", tag, "-m", "pip", "freeze", "--all"])
    return result.returncode, result.stdout


def freeze_pins(freeze: str) -> dict[str, str]:
    """`{normalised name: version}` for the lines the lock is supposed to govern."""
    pins: dict[str, str] = {}
    for line in freeze.splitlines():
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        if normalise(name) in {normalise(n) for n in NOT_A_DEPENDENCY}:
            continue
        pins[normalise(name)] = version.strip()
    return pins


def other_forms(freeze: str) -> list[str]:
    """Freeze lines that are not pins and not the excluded base-image/project entries.

    An `egg=`, `@ url` or VCS line here means something got installed from outside the lock's
    reach, which no version comparison would notice.
    """
    forms: list[str] = []
    for line in freeze.splitlines():
        if not line.strip() or "==" in line:
            continue
        name = re.split(r"[@#]", line, maxsplit=1)[0].strip()
        if normalise(name.removeprefix("-e ").strip()) in {normalise(n) for n in NOT_A_DEPENDENCY}:
            continue
        forms.append(line)
    return forms


# --------------------------------------------------------------------------- the checks


def verify_image(tag: str, lock: dict[str, str], label: str) -> None:
    """Claims 2 and 3, for one image."""
    rc, freeze = image_freeze(tag)
    if rc:
        check(f"{label}: freeze readable", False, f"pip freeze exited {rc}")
        return
    found = freeze_pins(freeze)
    offenders = {n: v for n, v in found.items() if lock.get(n) != v}
    check(
        f"{label}: every installed package is pinned in the lock at that version",
        not offenders,
        f"{len(found)} installed, {len(offenders)} disagree"
        + (": " + ", ".join(f"{n} installed {v}, lock says {lock.get(n, 'nothing')}"
                              for n, v in sorted(offenders.items())[:6]) if offenders else ""),
    )
    stray = other_forms(freeze)
    check(f"{label}: no install form outside `name==version`", not stray, "; ".join(stray) or "all pins")
    missing = {
        n: TARGET_PLATFORM_PINS[n][0]
        for n in TARGET_PLATFORM_PINS
        if n not in found or found[n] != TARGET_PLATFORM_PINS[n][0]
    }
    check(
        f"{label}: the platform-conditional pins earn their place",
        not missing,
        f"{', '.join(f'{n} (lock says {v})' for n, v in sorted(missing.items()))}"
        if missing
        else f"{', '.join(sorted(TARGET_PLATFORM_PINS))} present as pinned",
    )


def verify_licence_gate(tag: str) -> None:
    """Claim 4: FC-1 on the shipping platform, and proof the host scan cannot substitute for it."""
    rc, out = run_and_capture(
        ["docker", "run", "--rm", "--entrypoint", "python", tag,
         "-m", "synthverify.cli", "licenses", "--project-root", "/app", "--groups", "core,vision"]
    )
    lines = [line for line in out.splitlines()]
    header = next((line for line in lines if "FC-1 dependency licence scan" in line), "no header")
    graded = [line for line in lines if re.search(r"\bgreenlet\b", line)]
    check(
        "FC-1 inside the image passes on the image's own closure",
        rc == 0 and "RESULT: PASS" in out,
        f"exit {rc}; {header.strip()}",
    )
    check(
        "the image-side scan grades greenlet, which linux installs",
        bool(graded),
        graded[0].strip() if graded else "greenlet absent from the scan output",
    )

    host = run_and_capture([sys.executable, "-m", "synthverify.cli", "licenses"])
    host_sees = bool(re.search(r"\bgreenlet\b", host[1]))
    host_header = next((ln for ln in host[1].splitlines() if "FC-1 dependency licence scan" in ln), "?")
    if host_sees:
        # The host is a platform where greenlet really is in scope (linux x86_64/aarch64), so "the host
        # cannot see it" is not a witness this machine can offer - it was written from a darwin arm64
        # laptop. The non-redundancy claim that survives on every platform is the weaker, true one:
        # the image-side run grades the *artifact's* closure (core + vision, 13 declared roots) while
        # this host grades the declared closure of a development install (all extras, 22 roots). Two
        # different package sets, so the image run is about the shipped image, not a replay of the host.
        counts = re.search(r"(\d+) packages from (\d+) declared roots", host_header)
        image_counts = re.search(r"(\d+) packages from (\d+) declared roots", header)
        check(
            "this host is a greenlet platform, so the image run must grade a different closure",
            host[0] == 0 and bool(counts and image_counts) and counts.groups() != image_counts.groups(),
            f"host [{counts.group(0) if counts else '?'}] vs image "
            f"[{image_counts.group(0) if image_counts else '?'}]",
        )
    else:
        check(
            "the host scan genuinely does not see it (so the image run is not theatre)",
            host[0] == 0,
            f"{host_header.strip()}; greenlet absent from host output",
        )


def run_and_capture(cmd: list[str]) -> tuple[int, str]:
    result = run(cmd)
    return result.returncode, result.stdout + result.stderr


# --------------------------------------------------------------------------- mutations


def mutate_offline(mode: str) -> bool:
    """The consistency check must reject a lock that disagrees with the project.

    Run against copies in a temporary directory: the committed lock and pyproject.toml are not
    touched, so an interrupted run cannot leave the repo half-edited. Returns whether the tool
    rejected the doctored state - here its own exit code is the verdict, not a failed check.
    """
    with tempfile.TemporaryDirectory(prefix="sv-lock-") as tmp:
        tmpdir = Path(tmp)
        if mode == "bump-pin":
            lock_copy = tmpdir / "lock.txt"
            text = LOCK_PATH.read_text()
            bumped = re.sub(
                rf"^{SAMPLE_PACKAGE}==\S+$", f"{SAMPLE_PACKAGE}=={SAMPLE_DOCTORED}", text, count=1, flags=re.M
            )
            assert bumped != text, f"the lock does not pin {SAMPLE_PACKAGE}: the mutation would be a no-op"
            lock_copy.write_text(bumped)
            rc, out = run_and_capture(
                [sys.executable, "-m", "synthverify.cli", "dependency-lock", "--lock", str(lock_copy)]
            )
            named = SAMPLE_PACKAGE in out and "DRIFT" in out
            print(_issue(out, SAMPLE_PACKAGE) or out.strip().splitlines()[-1])
            return rc == 1 and named
        # new-dep
        project = tmpdir / "pyproject.toml"
        text = (REPO / "pyproject.toml").read_text()
        edited = text.replace('"pillow>=10.0",', f'"pillow>=10.0",\n    "{SAMPLE_NEW_DEP}",', 1)
        assert edited != text, "could not inject the extra declared requirement"
        project.write_text(edited)
        rc, out = run_and_capture(
            [
                sys.executable, "-m", "synthverify.cli", "dependency-lock",
                "--project-root", str(tmpdir), "--lock", str(LOCK_PATH),
            ]
        )
        named = "pg8000" in out.lower() and "UNPINNED" in out
        print(_issue(out, "pg8000") or out.strip().splitlines()[-1])
        return rc == 1 and named


def _verdict(out: str) -> str:
    return next((line.strip() for line in out.splitlines() if "RESULT:" in line), "no verdict printed")


def _issue(out: str, needle: str) -> str:
    return next((line.strip() for line in out.splitlines() if needle in line), "")


def _mutation_verdict(caught: bool, mode: str) -> int:
    print("RESULT:", f"MUTATION CAUGHT ({mode})" if caught
          else f"MUTATION NOT CAUGHT ({mode} reached the artifact and the gate accepted it)")
    return 0 if caught else 1


# --------------------------------------------------------------------------- main run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--mutate", choices=("doctored-pin", "no-lock", "bump-pin", "new-dep"))
    parser.add_argument("--expect-fail", action="store_true")
    args = parser.parse_args()

    tags: list[str] = []
    caught: bool | None = None
    try:
        pins, issues = read_lock(LOCK_PATH)
        lock = {key: pin.version for key, pin in pins.items()}
        check("the committed lock parses as pins only", not issues, f"{len(lock)} pins"
              + (f"; {issues[0].detail}" if issues else ""))

        if args.mutate in ("bump-pin", "new-dep"):
            caught = mutate_offline(args.mutate)
        elif args.mutate:
            doctored = f"# mutation: {args.mutate} - no pins the image could consult\n"
            if args.mutate == "doctored-pin":
                text = LOCK_PATH.read_text()
                doctored = re.sub(
                    rf"^{SAMPLE_PACKAGE}==\S+$", f"{SAMPLE_PACKAGE}=={SAMPLE_DOCTORED}", text, count=1, flags=re.M
                )
            tag = "synthverify:lock-mut"
            tags.append(tag)
            with doctored_lock(doctored) as lock_path:
                rc, _ = build(tag, lock=lock_path)
            check(f"{args.mutate}: the build itself succeeds", rc == 0, "so the gate, not pip, must catch it")
            if rc:
                # pip rejecting the injected file is not the gate working: print the verdict that
                # did happen and leave, so a broken mutation cannot be read as a caught one.
                print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
                return _mutation_verdict(False, args.mutate)
            _, freeze = image_freeze(tag)
            installed = freeze_pins(freeze).get(SAMPLE_PACKAGE)
            check(
                f"{args.mutate}: the image installed what the injected file said (so `-c` is wired, "
                "not decorative)",
                installed is not None and (
                    installed == SAMPLE_DOCTORED if args.mutate == "doctored-pin" else installed != lock[SAMPLE_PACKAGE]
                ),
                f"{SAMPLE_PACKAGE} in the image: {installed}; committed pin: {lock[SAMPLE_PACKAGE]}",
            )
            verify_image(tag, lock, args.mutate)
            caught = bool(FAILURES)
        else:
            tag_a, tag_b = "synthverify:lock-a", "synthverify:lock-b"
            tags += [tag_a, tag_b]
            rc_a, _ = build(tag_a)
            rc_b, _ = build(tag_b)
            check("two cold builds succeed", rc_a == 0 and rc_b == 0, f"exit {rc_a} / {rc_b}")
            if rc_a or rc_b:
                return 1

            rc_a, freeze_a = image_freeze(tag_a)
            rc_b, freeze_b = image_freeze(tag_b)
            check(
                "two cold builds print byte-identical `pip freeze --all`",
                freeze_a == freeze_b and rc_a == rc_b == 0,
                f"{len(freeze_a.splitlines())} lines each"
                + ("" if freeze_a == freeze_b else f"; {len(set(freeze_a.splitlines()) ^ set(freeze_b.splitlines()))} differ"),
            )
            if freeze_a != freeze_b:
                diff = sorted(set(freeze_a.splitlines()) ^ set(freeze_b.splitlines()))
                print("  first differences:", "; ".join(diff[:8]))

            verify_image(tag_a, lock, "image")
            verify_licence_gate(tag_a)
            dockerfile = DOCKERFILE.read_text()
            wired = any(
                line.lstrip().startswith("RUN") and "pip install" in line and "-c requirements-lock.txt" in line
                for line in dockerfile.splitlines()
            )
            check("the Dockerfile consumes the lock as a constraint", wired, "pip install -c requirements-lock.txt")
    finally:
        if not args.keep:
            for tag in tags:
                run(["docker", "rmi", "-f", tag])
        left = run(["docker", "images", "--filter", "reference=synthverify:lock-*", "--format", "{{.Repository}}:{{.Tag}}"])
        print(f"images left behind: {left.stdout.strip() or 'none'}"
              + ("" if args.keep else " (this run removed its own tags)"))

    print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
    for line in FAILURES:
        print("  FAILED:", line)
    if args.mutate:
        return _mutation_verdict(bool(caught), args.mutate)
    if args.expect_fail:
        print(
            "RESULT:",
            f"MUTATION CAUGHT ({len(FAILURES)} check(s) failed)"
            if FAILURES
            else "MUTATION NOT CAUGHT (the gate accepted a broken artifact)",
        )
        return 0 if FAILURES else 1
    print("RESULT:", "PASS" if not FAILURES else "FAIL")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
