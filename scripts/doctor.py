#!/usr/bin/env python3
"""First-run readiness report: name what is missing, and name the command that fixes it.

This exists because the gates in this repo *fail closed on purpose* - FC-1 treats a declared
dependency it cannot inspect as a violation, not a pass - so an incompletely installed
environment shows a first-time user fourteen assertion traces instead of one sentence. Every
check here either reports `FAIL` with the fix or stays quiet; advisory items (an optional system
codec, a database that is not running) are `WARN` and never affect the exit code.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "docker" / "requirements-lock.txt"

OK, WARN, FAIL = "ok", "warn", "fail"


class Report:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, subject: str, detail: str) -> None:
        self.rows.append((status, subject, detail))

    def ok(self, subject: str, detail: str = "") -> None:
        self.add(OK, subject, detail)

    def warn(self, subject: str, detail: str) -> None:
        self.add(WARN, subject, detail)

    def fail(self, subject: str, detail: str) -> None:
        self.add(FAIL, subject, detail)

    def emit(self) -> int:
        for status, subject, detail in self.rows:
            if self.quiet and status != FAIL:
                continue
            print(f"[{status:<4}] {subject:<26} {detail}".rstrip())
        failures = [row for row in self.rows if row[0] == FAIL]
        if self.quiet and not failures:
            return 0
        if failures:
            print(f"\n{len(failures)} thing(s) stand between this checkout and `make test`.")
        else:
            print("\nEnvironment is ready: `make test`, then `make run`.")
        return 1 if failures else 0


def requirement_name(spec: str) -> str:
    """`valkey>=6.0,<7 ; extra == "x"` -> `valkey`, without importing a PEP 508 parser.

    Doctor has to work in an environment whose dependencies are precisely what is in question,
    so it cannot depend on `packaging` being installed. Names only ever precede a relational
    operator, a marker semicolon, or an extras bracket.
    """
    return re.split(r"[<>=!;\[\s]", spec.strip(), maxsplit=1)[0]


def declared_groups() -> dict[str, list[str]]:
    """Every distribution this project declares, keyed by the root that declares it."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    groups = {"core": list(project.get("dependencies", []))}
    groups.update({extra: list(reqs) for extra, reqs in project.get("optional-dependencies", {}).items()})
    groups["build-system"] = list(pyproject["build-system"].get("requires", []))
    return groups


def check_interpreter(report: Report) -> None:
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    match = re.search(r"(\d+)\.(\d+)", requires)
    floor = tuple(int(part) for part in match.groups()) if match else (3, 10)
    if sys.version_info < floor:
        report.fail(
            "python version",
            f"running {sys.version.split()[0]}, `requires-python = \"{requires}\"`. "
            "Re-run with one that fits: `make setup PYTHON=/path/to/python3.11`",
        )
        return
    report.ok("python version", f"{sys.version.split()[0]} (needs >= {'.'.join(map(str, floor))})")


def check_self_contained(
    report: Report,
    *,
    prefix: Path | None = None,
    base: Path | None = None,
    pyvenv_cfg: str | None = None,
) -> None:
    """Grade the interpreter that is actually running, in either of its two shapes.

    The `prefix == base` case is *not* a failure: `docker/Dockerfile` installs into the image's own
    interpreter with no venv at all, and CI does the same through `setup-python`. Failing closed
    there would tell an operator that a supported deployment is broken - which is worse than the
    silent hole this file was written to catch. What is a failure is a venv that inherits the
    system's site-packages: it lets an un-locked package satisfy a pin here and fail on another
    machine, so the environment that passed `make test` is not the environment that ships.

    The three keyword arguments exist so the tests can drive every branch, including the
    inheritance one, instead of only observing whichever venv happens to be running them.
    """
    prefix = Path(sys.prefix) if prefix is None else prefix
    base = Path(sys.base_prefix) if base is None else base
    if prefix == base:
        report.warn(
            "virtualenv",
            f"no venv - grading {sys.executable}, which is how the image and CI are built. "
            "`make setup` creates .venv/ if you want the checkout to be self-contained",
        )
        return
    cfg = prefix / "pyvenv.cfg"
    text = (cfg.read_text() if cfg.is_file() else "") if pyvenv_cfg is None else pyvenv_cfg
    report.ok("virtualenv", f"{prefix}")
    if re.search(r"include-system-site-packages\s*=\s*true", text):
        report.fail(
            "site-packages isolation",
            f"{cfg} inherits the system interpreter, so an un-locked package can satisfy a pin "
            "here and fail on another machine. Recreate it: `rm -rf .venv && make setup`",
        )
    else:
        report.ok("site-packages isolation", f"self-contained ({prefix.name})")


def check_extras_installed(report: Report) -> None:
    groups = declared_groups()
    installed = installed_names()
    wanted = {
        group: {requirement_name(spec).lower().replace("_", "-") for spec in specs}
        for group, specs in groups.items()
    }
    missing = {group: sorted(names - installed) for group, names in wanted.items()}
    if not any(missing.values()):
        declared = {name for names in wanted.values() for name in names}
        report.ok("declared dependencies", f"all {len(declared)} roots installed")
        return
    every_extra = ",".join(sorted(key for key in groups if key not in ("core", "build-system")))
    for group, names in sorted(missing.items()):
        if not names:
            continue
        report.fail(
            f"{group} not installed",
            f"missing: {', '.join(names)}. The licence and lock gates grade this root and fail closed "
            "when they cannot see it, which is what `make test` reports. Fix: `make setup` "
            f'(installs ".[{every_extra}]", the same set CI installs)',
        )


def installed_names() -> set[str]:
    """Every distribution this interpreter can see, normalised the way PEP 503 normalises names."""
    return {
        name.lower().replace("_", "-")
        for name in (dist.metadata["Name"] for dist in importlib.metadata.distributions())
        if name
    }


def check_lock(report: Report) -> None:
    """Reuse the gate instead of restating it: `dependency-lock` is the authority on the pins."""
    if not LOCK.is_file():
        report.fail("dependency lock", f"{LOCK.relative_to(ROOT)} is missing from this checkout")
        return
    proc = subprocess.run(
        [sys.executable, "-m", "synthverify.cli", "dependency-lock"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    first_problem = next((line for line in (proc.stdout + proc.stderr).splitlines() if "FAIL" in line or "fail" in line.lower()), "")
    if proc.returncode == 0:
        report.ok("dependency lock", f"{len(LOCK.read_text().splitlines())} lines, matches pyproject.toml")
    else:
        report.fail(
            "dependency lock",
            f"`make lock-check` exits {proc.returncode} in this environment. {first_problem}".strip(),
        )


def check_advisories(report: Report) -> None:
    """Things that change what you can *do*, not whether the build is sound."""
    if shutil.which("ffmpeg"):
        report.ok("ffmpeg", f"{shutil.which('ffmpeg')} (non-WAV audio decoding)")
    else:
        report.warn(
            "ffmpeg",
            "not on PATH: 8/16/24/32-bit PCM WAV decodes natively, MP3/AAC/Ogg do not. "
            "`brew install ffmpeg` / `apt install ffmpeg` if you want the whole audio path",
        )

    env_url = os.environ.get("SV_TEST_POSTGRES_URL", "")
    if env_url:
        report.ok("SV_TEST_POSTGRES_URL", "set: the Postgres half of AC-INFRA-1 runs instead of skipping")
    else:
        report.warn(
            "SV_TEST_POSTGRES_URL",
            "unset: 16 Postgres-gated tests skip (the suite still passes). Start a server with "
            "`docker run -d --name sv-pg16 -p 5432:5432 -e POSTGRES_USER=sv -e POSTGRES_PASSWORD=sv "
            "-e POSTGRES_DB=sv_test postgres:16`, and note the CI-only driver `pip install pg8000`",
        )

    if os.environ.get("SV_TEST_VALKEY"):
        report.ok("SV_TEST_VALKEY", "set: the shared-bucket tests run instead of skipping")
    else:
        report.warn("SV_TEST_VALKEY", "unset: 5 Valkey-gated tests skip. `make valkey-up` and set it to 127.0.0.1:6379")

    report.ok("media + database paths", str(ROOT / "data") + " (created on first run; git-ignored)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="print only the lines that need action")
    args = parser.parse_args(argv)

    report = Report(args.quiet)
    check_interpreter(report)
    check_self_contained(report)
    check_extras_installed(report)
    check_lock(report)
    check_advisories(report)
    return report.emit()


if __name__ == "__main__":
    raise SystemExit(main())
