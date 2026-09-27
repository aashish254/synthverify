"""T39: the dependency lock and the check that keeps it honest.

The gate has two halves, like FC-1. The first proves the *check* is not vacuous: a lock that is
missing a pin, carries a range instead of a pin, disagrees with the installed closure or has
forgotten a package must fail, and the failure must name the package. The second asserts the
committed artifact of this repository is internally consistent - which is what CI runs, and which
`scripts/lock_e2e.py` corroborates against a built image rather than against these fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synthverify.cli import main as cli_main
from synthverify.compliance.dependency_lock import (
    HEADER,
    LOCK_PATH,
    NOT_A_DEPENDENCY,
    TARGET_PLATFORM_PINS,
    Pin,
    check_lock,
    closure_versions,
    read_lock,
    write_lock,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def lock_text(*pins: str, header: str = "# test lock\n") -> str:
    return header + "\n".join(pins) + "\n" if pins else header


class TestReadLock:
    def test_a_pin_reads_as_a_pin(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text(lock_text("fastapi==0.141.1", "typing_extensions==4.16.0"))
        pins, issues = read_lock(lock)
        assert issues == []
        assert pins["fastapi"] == Pin("fastapi", "0.141.1")
        assert pins["typing-extensions"].key == "typing-extensions"

    @pytest.mark.parametrize(
        "line",
        [
            "fastapi>=0.141",           # a range is not a pin
            "fastapi~=0.141.0",
            "fastapi!=0.141.1",
            'fastapi==0.141.1 ; extra == "dev"',  # a condition is not a pin
            "fastapi==",                # a pin to nothing
            "hg+https://example/x",     # not a `name==version` line at all
        ],
    )
    def test_anything_that_is_not_a_pin_is_rejected(self, tmp_path: Path, line: str):
        lock = tmp_path / "lock.txt"
        lock.write_text(lock_text(line))
        pins, issues = read_lock(lock)
        assert [i.kind for i in issues] == ["unpinned"], pins
        assert line in issues[0].detail

    def test_pinning_the_same_package_twice_is_a_problem(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text(lock_text("numpy==1.26.4", "numpy==2.4.6"))
        _, issues = read_lock(lock)
        assert "duplicate" in {i.kind for i in issues}

    def test_a_missing_lock_means_the_image_installs_unpinned(self, tmp_path: Path):
        _, issues = read_lock(tmp_path / "nope.txt")
        assert [i.kind for i in issues] == ["missing"]
        assert "installs unpinned" in issues[0].detail

    def test_comments_and_blank_lines_are_not_packages(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text("# why\n\n   # also why\nnumpy==2.4.6\n")
        pins, issues = read_lock(lock)
        assert issues == [] and list(pins) == ["numpy"]


class TestClosureVersions:
    def test_every_declared_group_is_counted(self):
        """The answer to "did this check see the extra I just added?" has to be printed."""
        _, groups = closure_versions(REPO_ROOT)
        assert {"core", "vision", "dev", "valkey"} == set(groups)
        assert groups["vision"] >= 2

    def test_the_build_backend_requirements_end_up_locked(self):
        """`[build-system] requires` is not a project dependency, so the closure walk never sees
        it and the lock would silently skip the toolchain that builds the wheel. FC-1 grades
        build-time dependencies, so the pair has to be reachable anyway - `dev` declares it, and
        this is the check that keeps that true."""
        import tomllib

        from synthverify.compliance.licenses import parse_requirement

        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            build_requires = tomllib.load(handle)["build-system"]["requires"]
        pins, _ = read_lock(LOCK_PATH)
        assert build_requires, "the fixture assumed a declared build backend"
        for spec in build_requires:
            key = parse_requirement(spec).key
            assert key in pins, f"{spec} is a build dependency but has no pin"

    def test_a_declared_requirement_that_is_not_installed_has_no_version(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "demo"\ndependencies = ["definitely-not-installed-xyz"]\n',
            encoding="utf-8",
        )
        versions, _ = closure_versions(tmp_path)
        assert versions.get("definitely-not-installed-xyz") == ""


class TestCheckLock:
    def test_a_complete_lock_passes(self):
        assert check_lock(REPO_ROOT, LOCK_PATH).ok

    def test_dropping_a_pin_is_reported_as_unpinned(self, tmp_path: Path):
        good = LOCK_PATH.read_text()
        kept = [line for line in good.splitlines() if not line.startswith("numpy==")]
        broken = tmp_path / "lock.txt"
        broken.write_text("\n".join(kept) + "\n")
        report = check_lock(REPO_ROOT, broken)
        assert [i.kind for i in report.issues if i.detail.startswith("numpy")] == ["unpinned"]

    def test_a_changed_pin_is_reported_as_drift(self, tmp_path: Path):
        broken = tmp_path / "lock.txt"
        broken.write_text(LOCK_PATH.read_text().replace("anyio==4.14.2", "anyio==4.15.1", 1))
        report = check_lock(REPO_ROOT, broken)
        drift = [i for i in report.issues if i.kind == "drift"]
        assert [i.detail for i in drift] == [
            "anyio: lock says 4.15.1, installed closure says 4.14.2"
        ]

    def test_a_pin_nothing_declares_is_stale(self, tmp_path: Path):
        stale = tmp_path / "lock.txt"
        stale.write_text(LOCK_PATH.read_text() + "some-forgotten-package==1.2.3\n")
        report = check_lock(REPO_ROOT, stale)
        assert [i.kind for i in report.issues] == ["stale"]
        assert "some-forgotten-package" in report.issues[0].detail

    def test_a_base_image_tool_is_stale_but_says_what_it_is(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text(LOCK_PATH.read_text() + "pip==24.0\n")
        detail = check_lock(REPO_ROOT, lock).issues[0].detail
        assert "base-image tool" in detail

    def test_the_platform_pin_is_exempt_from_stale_and_required_to_be_present(self, tmp_path: Path):
        """`greenlet` cannot be discovered from a darwin host, so it is declared in the module
        instead: that exemption must not rot into a free-for-all, so the file still has to carry
        the pin, and at the version the declaration states.

        Asserted by *which package* is complained about rather than by how many complaints there
        are. On a host where SQLAlchemy's marker does pull `greenlet` into the closure - Linux and
        Windows, i.e. every `make scale`/CI platform and not the one this file was written on -
        removing the pin is legitimately two findings: the exemption went missing, *and* an
        installed package has no pin. Both name the same package, which is the whole claim; a bare
        `== ["unpinned"]` was true only on Apple Silicon and failed the portability leg everywhere
        else.
        """
        without = tmp_path / "without.txt"
        without.write_text(
            "\n".join(
                line for line in LOCK_PATH.read_text().splitlines() if not line.startswith("greenlet==")
            )
            + "\n"
        )
        issues = check_lock(REPO_ROOT, without).issues
        assert [i.kind for i in issues if "ships in the image" in i.detail] == ["unpinned"], issues
        assert {i.detail.split()[0].rstrip(":") for i in issues} == {"greenlet"}, issues

        disagreed = tmp_path / "disagreed.txt"
        disagreed.write_text(LOCK_PATH.read_text().replace("greenlet==3.5.6", "greenlet==9.9.9", 1))
        moved = check_lock(REPO_ROOT, disagreed).issues
        assert [i.detail.split()[0].rstrip(":") for i in moved] == ["greenlet"], moved
        # Either report quotes both sides, so the reader can tell which one to fix.
        assert all(("9.9.9" in i.detail and "3.5.6" in i.detail) for i in moved), moved
        # Which comparison catches it is host-dependent. Where SQLAlchemy's marker pulls
        # `greenlet` into the installed closure (Linux, Windows) the lock-vs-closure check fires
        # as `drift` and the exempt-pin check is skipped because the name *is* in the closure; on
        # Apple Silicon, where it is not, `target-drift` is the only possible report.
        assert [i.kind for i in moved] == ["drift"] or [i.kind for i in moved] == ["target-drift"], moved

    def test_the_exclusions_are_the_ones_the_script_relies_on(self):
        assert {"pip", "setuptools", "wheel", "synthverify"} == set(NOT_A_DEPENDENCY)
        assert "greenlet" in TARGET_PLATFORM_PINS
        # Every exempt pin is actually in the file, or the exemption hides a missing pin.
        pins, _ = read_lock(LOCK_PATH)
        assert all(name in pins for name in TARGET_PLATFORM_PINS)

    def test_the_header_explains_itself(self):
        text = LOCK_PATH.read_text()
        assert text.startswith(HEADER.splitlines()[0])
        for phrase in ("constraints", "Dockerfile.postgres", "greenlet", "PEP 517"):
            assert phrase in text


class TestWriteLock:
    def test_regenerating_from_a_complete_environment_round_trips(self, tmp_path: Path):
        written = tmp_path / "lock.txt"
        report = write_lock(REPO_ROOT, written)
        assert report.ok, report.format_text()
        assert read_lock(written)[0] == read_lock(LOCK_PATH)[0]

    def test_it_refuses_to_write_a_lock_with_holes_in_it(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "demo"\ndependencies = ["definitely-not-installed-xyz"]\n',
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="not installed here"):
            write_lock(tmp_path, tmp_path / "lock.txt")


class TestCli:
    def test_dependency_lock_passes_on_the_committed_files(self, capsys: pytest.CaptureFixture[str]):
        assert cli_main(["dependency-lock", "--project-root", str(REPO_ROOT), "--lock", str(LOCK_PATH)]) == 0
        assert "RESULT: PASS" in capsys.readouterr().out

    def test_json_output_names_the_platform_pin(self, capsys: pytest.CaptureFixture[str]):
        assert cli_main(["dependency-lock", "--json"]) == 0
        payload = capsys.readouterr().out
        assert '"greenlet"' in payload and '"ok": true' in payload

    def test_a_broken_lock_exits_nonzero(self, tmp_path: Path):
        broken = tmp_path / "lock.txt"
        broken.write_text("# nothing pinned\n")
        assert cli_main(["dependency-lock", "--lock", str(broken)]) == 1

    def test_freedom_includes_the_lock_check(self):
        """`make freedom` is the one command a reviewer runs; if the lock check is not in it,
        the gate exists on paper only."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "synthverify.cli", "freedom"],
            capture_output=True, text=True, cwd=REPO_ROOT, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Dependency lock:" in result.stdout
