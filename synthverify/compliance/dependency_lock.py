"""T39: the dependency lock, and the check that keeps it honest.

`docker/Dockerfile` used to end in `pip install ".[vision]"`, which means the shipped image
was whatever PyPI considered newest at build time. `AC-FC-6`'s "deploy from source" was met
and a customer could not tell which build they had. This module owns the fix: a committed
lock of *pins* that the image installs with, and a consistency check that fails when
`pyproject.toml` and the lock disagree.

Two design points worth stating, because they decide what this can and cannot prove.

**The lock is a constraints file, not an install list.** `pip install -c lock.txt ".[vision]"`
applies only the pins relevant to the requested extra and ignores the rest, so one file can
serve the image (`core` + `vision`), the dev environment, and every CI leg (`dev`, `valkey`).
That is why `pip install -e ".[vision]"` and `pip install -e ".[vision,dev,valkey]"` can share
one lock: an unmatched constraint is inert, and a matched one is a fact.

**What the check compares is the *declared closure*, not a PyPI re-resolution.** The same
offline metadata walk `FC-1` uses (`licenses.dependency_closure`) gives the package set this
project actually pulls in, from packages actually installed here. So the check needs no
network - and it inherits one requirement from that choice: it must run where every declared
extra is installed, because an extra nobody installed has a closure nobody can read. That is
the `freedom` CI leg, and `make lock-check` locally.

Which also decides *where a regenerated lock may come from*: an environment pip resolved, not
one edited by hand. A virtualenv carrying an inconsistent pair copies straight into the lock -
the first version of this file pinned `numpy==1.26.4` next to an `opencv-python` whose own
metadata demands `numpy>=2`, and the image build was the check that said so (`pip` refuses what
the installed metadata merely permitted to coexist).

The scope is deliberately narrow (spec §6.1, "not claimed" bullet): this makes the *package
set* deterministic. It does not pin the base image by digest, so the interpreter's own `pip`
and layer timestamps still differ between builds. One subtlety the checks state rather than
hide: `-c` does not reach a PEP 517 *isolated build environment*, so the backend the image
build uses is resolved by pip, not by this file - `setuptools` and `wheel` are locked here
because `pyproject.toml` declares them (and FC-1 grades build-time deps offline, so they must
be installed where the gate runs), and `scripts/lock_e2e.py` proves what the lock does govern:
the package set that ends up inside the image.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthverify.compliance.licenses import (
    dependency_closure,
    installed_distributions,
    parse_requirement,
)

LOCK_PATH = Path("docker/requirements-lock.txt")

#: Names a *container freeze* carries that these pins do not govern: the base image's own
#: packaging tools (never installed by a requirement, so `-c` cannot move them) and this project.
#: `scripts/lock_e2e.py` excludes exactly these when it compares `pip freeze --all` with the lock.
#: `setuptools` and `wheel` are pinned *in* the lock, because `pyproject.toml` declares them and
#: the gate environment must be able to grade them; inside the image they are the base tag's
#: copies, which is why they are excluded from the comparison but not from the file.
NOT_A_DEPENDENCY = ("pip", "setuptools", "wheel", "synthverify")

#: Pins the deployment image needs and no host-side walk can discover, because the marker that
#: pulls them in is false on the machine running the check. Each entry states the marker, so the
#: reason survives whoever reads it next. `scripts/lock_e2e.py` checks these both ways: the
#: package must appear in the image's freeze (or the exemption is unearned), and the version must
#: match the pin (or the platform-conditional fact has drifted out of the file).
TARGET_PLATFORM_PINS: dict[str, tuple[str, str]] = {
    "greenlet": (
        "3.5.6",
        'SQLAlchemy requires `greenlet>=1` on `platform_machine == "aarch64"`/x86_64 - a linux '
        "image; darwin arm64 is not in that list, so the host closure walk never sees it",
    ),
}

HEADER = """\
# SynthVerify dependency lock - every package this project can pull in, pinned.
#
# Consumed as a *constraints* file, so one lock serves every install profile:
#   docker/Dockerfile          pip install -c docker/requirements-lock.txt ".[vision]"
#   make setup / CI            pip install -c docker/requirements-lock.txt -e ".[vision,dev,valkey]"
# An entry that the requested extra does not need is inert; one it does need is a fact.
#
# Generated and verified by:  ./.venv/bin/python -m synthverify.cli dependency-lock [--write]
# That check walks the declared closure offline (the same metadata FC-1 grades), so generate it
# from an environment pip resolved from this file, not one edited by hand: a hand-edited env
# copies its own inconsistencies straight into the pins.
#
# Not pinned here, and named so nobody mistakes the silence for coverage: this project itself,
# and - inside the image only - `pip`, `setuptools` and `wheel`, which come from the base image
# tag rather than from a requirement. `psycopg` is installed by docker/Dockerfile.postgres from
# its own pin at deploy time (FC-1 does not grade it: it is a deployment overlay, not a declared
# dependency). `-c` also does not reach a PEP 517 *isolated build environment*, so the backend
# that builds this wheel is resolved by pip; the setuptools/wheel pins below govern the
# environment that runs the gates, where FC-1 can read their licences.
#
# One pin is here for the image and not for the machine that wrote this file: `greenlet`, which
# SQLAlchemy requires only on the linux platforms in its `platform_machine` marker. A host on
# darwin arm64 cannot discover it by walking installed metadata, so it is declared in
# dependency_lock.TARGET_PLATFORM_PINS with its reason - and scripts/lock_e2e.py checks it
# against the built image, which is where the fact lives.
"""


@dataclass(frozen=True)
class Pin:
    """One `name==version` line of the lock."""

    name: str
    version: str

    @property
    def key(self) -> str:
        requirement = parse_requirement(self.name)
        return requirement.key if requirement else self.name.lower()

    def __str__(self) -> str:
        return f"{self.name}=={self.version}"


@dataclass
class LockIssue:
    """One way the lock and the project can disagree."""

    kind: str
    detail: str


@dataclass
class LockReport:
    """The outcome of comparing the lock with the declared dependency closure."""

    pins: dict[str, Pin] = field(default_factory=dict)
    closure: dict[str, str] = field(default_factory=dict)  # key -> installed version
    groups: dict[str, int] = field(default_factory=dict)   # declared group -> requirement count
    issues: list[LockIssue] = field(default_factory=list)
    lock_path: str = str(LOCK_PATH)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "lock": self.lock_path,
            "pinned": len(self.pins),
            "closure": len(self.closure),
            "declared_groups": self.groups,
            "target_platform_pins": {
                name: {"version": version, "reason": reason}
                for name, (version, reason) in TARGET_PLATFORM_PINS.items()
            },
            "issues": [{"kind": i.kind, "detail": i.detail} for i in self.issues],
        }

    def format_text(self) -> str:
        groups = ", ".join(f"{name}={count}" for name, count in sorted(self.groups.items()))
        lines = [
            f"Dependency lock: {len(self.closure)} packages in the declared closure, "
            f"{len(self.pins)} pinned in {self.lock_path}"
        ]
        lines.append(f"  declared groups: {groups}")
        lines.append(
            "  not locked, on purpose: this project, and - inside the image - `pip`, "
            "`setuptools` and `wheel` from the base tag, plus `psycopg`, which "
            "docker/Dockerfile.postgres installs from its own pin at deploy time - see the "
            f"header of {self.lock_path}. scripts/lock_e2e.py compares a built container's "
            "freeze with these pins under exactly these exclusions."
        )
        for name, (version, reason) in sorted(TARGET_PLATFORM_PINS.items()):
            lines.append(f"  pinned for the image, not this host: {name}=={version} - {reason}")
        for issue in self.issues:
            lines.append(f"  {issue.kind.upper():<9} {issue.detail}")
        lines.append(
            "  RESULT: "
            + ("PASS" if self.ok else f"FAIL ({len(self.issues)} problem(s))")
        )
        return "\n".join(lines)


def closure_versions(project_root: Path | str = ".") -> tuple[dict[str, str], dict[str, int]]:
    """`{normalised name: installed version}` for everything the declared groups pull in.

    Also returns the per-group requirement counts, so a report can show *which* extras were
    considered - the answer to "did this check see the extra I just added?".
    """
    root = Path(project_root).resolve()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise FileNotFoundError(f"No pyproject.toml in {root}")
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})

    groups = {
        "core": list(project.get("dependencies", [])),
        **{name: list(reqs) for name, reqs in (project.get("optional-dependencies") or {}).items()},
    }
    roots = sorted(
        {r for reqs in groups.values() for r in (parse_requirement(s) for s in reqs) if r},
        key=lambda r: r.key,
    )
    index = installed_distributions()
    closure = dependency_closure(roots, index)
    versions = {key: (index[key][0].version or "") for key in sorted(closure) if index.get(key)}
    # A declared requirement that is not installed has no version to pin, which is a finding
    # rather than a silence: an uninstalled extra's closure is invisible.
    missing = {
        requirement.key
        for specs in groups.values()
        for spec in specs
        if (requirement := parse_requirement(spec)) and requirement.key not in closure
    }
    versions.update({key: "" for key in sorted(missing)})
    return versions, {name: len(reqs) for name, reqs in groups.items()}


def read_lock(path: Path | str = LOCK_PATH) -> tuple[dict[str, Pin], list[LockIssue]]:
    """Parse the lock, rejecting anything that is not a pin."""
    lock = Path(path)
    pins: dict[str, Pin] = {}
    issues: list[LockIssue] = []
    if not lock.is_file():
        issues.append(LockIssue("missing", f"{lock} does not exist: the image installs unpinned"))
        return pins, issues
    for number, raw in enumerate(lock.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line or any(op in line for op in ("<=", ">=", "!=", ">", "<", ";", " ")):
            issues.append(
                LockIssue("unpinned", f"{lock}:{number} is not a `name==version` pin: {raw!r}")
            )
            continue
        name, _, version = line.partition("==")
        pin = Pin(name.strip(), version.strip())
        if pin.key in pins:
            issues.append(LockIssue("duplicate", f"{lock}:{number} pins {pin.name} twice"))
        if not pin.version:
            issues.append(
                LockIssue("unpinned", f"{lock}:{number} pins {pin.name} to nothing: {raw!r}")
            )
        pins[pin.key] = pin
    return pins, issues


def check_lock(project_root: Path | str = ".", lock_path: Path | str = LOCK_PATH) -> LockReport:
    """Compare the committed lock with the declared closure, in both directions."""
    closure, groups = closure_versions(project_root)
    pins, issues = read_lock(lock_path)

    for key, version in sorted(closure.items()):
        pin = pins.get(key)
        name = pin.name if pin else key
        if pin is None:
            where = f"installed at {version}" if version else "declared but not installed here"
            issues.append(
                LockIssue("unpinned", f"{key} is in the closure ({where}) and has no lock entry")
            )
        elif not version:
            issues.append(
                LockIssue(
                    "not-installed",
                    f"{key} is pinned to {pin.version} but nothing was installed to compare: "
                    "install every declared extra before running this check",
                )
            )
        elif pin.version != version:
            issues.append(
                LockIssue(
                    "drift",
                    f"{name}: lock says {pin.version}, installed closure says {version}",
                )
            )
    for key, pin in sorted(pins.items()):
        if key in closure:
            continue
        if pin.name.lower() in TARGET_PLATFORM_PINS:
            # Declared for the image's platform, invisible to this host's metadata walk: an
            # earned exemption, not a silent one. scripts/lock_e2e.py checks it against freeze.
            expected = TARGET_PLATFORM_PINS[pin.name.lower()][0]
            if pin.version != expected:
                issues.append(
                    LockIssue(
                        "target-drift",
                        f"{pin.name}: the lock says {pin.version} and "
                        f"TARGET_PLATFORM_PINS says {expected} - one of the two is stale",
                    )
                )
            continue
        hint = " (a base-image tool, not a dependency)" if pin.name.lower() in NOT_A_DEPENDENCY else ""
        issues.append(
            LockIssue("stale", f"{pin} is in the lock and nothing declares it{hint}")
        )
    for name, (version, reason) in sorted(TARGET_PLATFORM_PINS.items()):
        if parse_requirement(name).key not in pins:
            issues.append(
                LockIssue(
                    "unpinned",
                    f"{name} ships in the image and the lock must pin it to {version}: {reason}",
                )
            )
    return LockReport(
        pins=pins, closure=closure, groups=groups, issues=issues, lock_path=str(lock_path)
    )


def write_lock(project_root: Path | str = ".", lock_path: Path | str = LOCK_PATH) -> LockReport:
    """Regenerate the lock from the installed closure. Refuses to write an incomplete one."""
    closure, groups = closure_versions(project_root)
    gaps = sorted(key for key, version in closure.items() if not version)
    if gaps:
        raise RuntimeError(
            "cannot write the lock: these declared requirements are not installed here, so "
            f"their versions are unknown: {', '.join(gaps)}. Install every extra first "
            '(`pip install -e ".[vision,dev,valkey]"`).'
        )
    pins = {key: Pin(key, version) for key, version in closure.items()}
    for name, (version, _reason) in TARGET_PLATFORM_PINS.items():
        pins.setdefault(parse_requirement(name).key, Pin(name, version))
    body = "\n".join(str(pin) for _, pin in sorted(pins.items(), key=lambda kv: kv[1].name.lower()))
    Path(lock_path).write_text(HEADER + body + "\n")
    return check_lock(project_root, lock_path)
