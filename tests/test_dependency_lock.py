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
    MATRIX_PLATFORM_PINS,
    NOT_A_DEPENDENCY,
    PLATFORM_PINS,
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
        lock.write_text(lock_text("fastapi==0.141.1", "typing_extensions==4.16.0"), encoding="utf-8")
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
        lock.write_text(lock_text(line), encoding="utf-8")
        pins, issues = read_lock(lock)
        assert [i.kind for i in issues] == ["unpinned"], pins
        assert line in issues[0].detail

    def test_pinning_the_same_package_twice_is_a_problem(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text(lock_text("numpy==1.26.4", "numpy==2.4.6"), encoding="utf-8")
        _, issues = read_lock(lock)
        assert "duplicate" in {i.kind for i in issues}

    def test_a_missing_lock_means_the_image_installs_unpinned(self, tmp_path: Path):
        _, issues = read_lock(tmp_path / "nope.txt")
        assert [i.kind for i in issues] == ["missing"]
        assert "installs unpinned" in issues[0].detail

    def test_comments_and_blank_lines_are_not_packages(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text("# why\n\n   # also why\nnumpy==2.4.6\n", encoding="utf-8")
        pins, issues = read_lock(lock)
        assert issues == [] and list(pins) == ["numpy"]


class TestClosureVersions:
    def test_every_declared_group_is_counted(self):
        """The answer to "did this check see the extra I just added?" has to be printed."""
        _, groups, _ = closure_versions(REPO_ROOT)
        assert {"core", "vision", "dev", "valkey", "jwt"} == set(groups)
        assert groups["vision"] >= 2
        # `jwt` exists so the OIDC bearer path (REQ-IDAM-1 / AC-IDAM-1) can be graded offline;
        # its closure is small (PyJWT + the cryptography it pulls), so a lower bound still catches
        # the extra silently dropping out of the walk.
        assert groups["jwt"] >= 1

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
        versions, _, _ = closure_versions(tmp_path)
        assert versions.get("definitely-not-installed-xyz") == ""

    def test_the_walk_reports_what_a_marker_hid_from_this_interpreter(self):
        """The evidence for an exemption is the requirement line itself, parent included.

        This is the half of the design that keeps a platform rule from becoming a table somebody
        forgets to maintain: `click` on this machine carries `colorama; platform_system == "Windows"`,
        and that line - not a hand-written allowance - is what makes a `colorama` pin here defensible
        on darwin and checkable on Windows.
        """
        _versions, _groups, inert = closure_versions(REPO_ROOT)
        assert "colorama" in inert
        assert "click" in inert["colorama"] and "Windows" in inert["colorama"]
        # A marker that *passes* here is not inert: uvloop is installed on darwin, so its pin is
        # compared against a real version rather than exempted.
        assert "uvloop" not in inert


class TestCheckLock:
    def test_a_complete_lock_passes(self):
        assert check_lock(REPO_ROOT, LOCK_PATH).ok

    def test_dropping_a_pin_is_reported_as_unpinned(self, tmp_path: Path):
        good = LOCK_PATH.read_text(encoding="utf-8")
        kept = [line for line in good.splitlines() if not line.startswith("numpy==")]
        broken = tmp_path / "lock.txt"
        broken.write_text("\n".join(kept) + "\n", encoding="utf-8")
        report = check_lock(REPO_ROOT, broken)
        assert [i.kind for i in report.issues if i.detail.startswith("numpy")] == ["unpinned"]

    def test_a_changed_pin_is_reported_as_drift(self, tmp_path: Path):
        broken = tmp_path / "lock.txt"
        broken.write_text(LOCK_PATH.read_text(encoding="utf-8").replace("anyio==4.14.2", "anyio==4.15.1", 1), encoding="utf-8")
        report = check_lock(REPO_ROOT, broken)
        drift = [i for i in report.issues if i.kind == "drift"]
        assert [i.detail for i in drift] == [
            "anyio: lock says 4.15.1, installed closure says 4.14.2"
        ]

    def test_a_pin_nothing_declares_is_stale(self, tmp_path: Path):
        stale = tmp_path / "lock.txt"
        stale.write_text(LOCK_PATH.read_text(encoding="utf-8") + "some-forgotten-package==1.2.3\n", encoding="utf-8")
        report = check_lock(REPO_ROOT, stale)
        assert [i.kind for i in report.issues] == ["stale"]
        assert "some-forgotten-package" in report.issues[0].detail

    def test_a_base_image_tool_is_stale_but_says_what_it_is(self, tmp_path: Path):
        lock = tmp_path / "lock.txt"
        lock.write_text(LOCK_PATH.read_text(encoding="utf-8") + "pip==24.0\n", encoding="utf-8")
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
                line for line in LOCK_PATH.read_text(encoding="utf-8").splitlines() if not line.startswith("greenlet==")
            )
            + "\n",
            encoding="utf-8",
        )
        issues = check_lock(REPO_ROOT, without).issues
        assert [i.kind for i in issues if "ships on another platform" in i.detail] == ["unpinned"], issues
        assert {i.detail.split()[0].rstrip(":") for i in issues} == {"greenlet"}, issues

        disagreed = tmp_path / "disagreed.txt"
        disagreed.write_text(LOCK_PATH.read_text(encoding="utf-8").replace("greenlet==3.5.6", "greenlet==9.9.9", 1), encoding="utf-8")
        moved = check_lock(REPO_ROOT, disagreed).issues
        assert [i.detail.split()[0].rstrip(":") for i in moved] == ["greenlet"], moved
        # Either report quotes both sides, so the reader can tell which one to fix.
        assert all(("9.9.9" in i.detail and "3.5.6" in i.detail) for i in moved), moved
        # Which comparison catches it is host-dependent. Where SQLAlchemy's marker pulls
        # `greenlet` into the installed closure (Linux, Windows) the lock-vs-closure check fires
        # as `drift` and the exempt-pin check is skipped because the name *is* in the closure; on
        # Apple Silicon, where it is not, `target-drift` is the only possible report.
        assert [i.kind for i in moved] == ["drift"] or [i.kind for i in moved] == ["target-drift"], moved

    def test_a_pin_another_platform_needs_and_this_one_cannot_install(self, tmp_path: Path):
        """`colorama` is the pin the first hosted Windows run died on.

        Two findings, both spurious on their face: on Windows it is in the closure with no lock entry
        (`unpinned`), and on darwin it is in the lock with no closure entry (`stale`). The fix had to
        be a pin the darwin host cannot verify the version of, so the version's authority is the
        Windows run itself - which is the `portability` matrix leg, and why both halves are asserted
        here rather than trusting the comment in the file.
        """
        pins, _ = read_lock(LOCK_PATH)
        assert "colorama" in pins, "the lock has to pin what a Windows install pulls in"
        assert pins["colorama"].version == MATRIX_PLATFORM_PINS["colorama"][0]
        assert check_lock(REPO_ROOT, LOCK_PATH).ok

        without = tmp_path / "without.txt"
        without.write_text(
            "\n".join(
                line for line in LOCK_PATH.read_text(encoding="utf-8").splitlines() if not line.startswith("colorama==")
            )
            + "\n",
            encoding="utf-8",
        )
        issues = [i for i in check_lock(REPO_ROOT, without).issues if "colorama" in i.detail]
        assert [i.kind for i in issues] == ["unpinned"], issues

        disagreed = tmp_path / "disagreed.txt"
        disagreed.write_text(
            LOCK_PATH.read_text(encoding="utf-8").replace("colorama==0.4.6", "colorama==0.3.9", 1),
            encoding="utf-8",
        )
        moved = [i for i in check_lock(REPO_ROOT, disagreed).issues if "colorama" in i.detail]
        assert [i.kind for i in moved] == ["target-drift"], moved
        assert "0.4.6" in moved[0].detail and "MATRIX_PLATFORM_PINS" in moved[0].detail

    def test_a_pin_inert_on_this_platform_is_exempted_with_its_evidence_printed(self, tmp_path: Path):
        """The `stale` direction may not accuse a pin this platform simply does not install.

        `valkey` requires `async-timeout` below python 3.11.3; this interpreter is newer, so nothing
        here installs it while a CI matrix on 3.11.0 would. The exemption is earned from the metadata
        line - and it is *reported*, because an exemption nobody can see is how a stale pin hides.
        """
        lock = tmp_path / "lock.txt"
        lock.write_text(LOCK_PATH.read_text(encoding="utf-8") + "async-timeout==5.4.2\n", encoding="utf-8")
        report = check_lock(REPO_ROOT, lock)
        assert report.ok, report.format_text()
        assert "async-timeout==5.4.2" in report.exempted
        assert "valkey" in report.exempted["async-timeout==5.4.2"]
        assert "async-timeout==5.4.2" in report.format_text()

    def test_the_exclusions_are_the_ones_the_script_relies_on(self):
        assert {"pip", "setuptools", "wheel", "synthverify"} == set(NOT_A_DEPENDENCY)
        assert "greenlet" in TARGET_PLATFORM_PINS
        assert set(PLATFORM_PINS) == set(TARGET_PLATFORM_PINS) | set(MATRIX_PLATFORM_PINS)
        # Every exempt pin is actually in the file, or the exemption hides a missing pin.
        pins, _ = read_lock(LOCK_PATH)
        assert all(name in pins for name in PLATFORM_PINS)

    def test_the_header_explains_itself(self):
        text = LOCK_PATH.read_text(encoding="utf-8")
        assert text.startswith(HEADER.splitlines()[0])
        for phrase in ("constraints", "Dockerfile.postgres", "greenlet", "colorama", "PEP 517"):
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
        broken.write_text("# nothing pinned\n", encoding="utf-8")
        assert cli_main(["dependency-lock", "--lock", str(broken)]) == 1

    def test_freedom_includes_the_lock_check(self):
        """`make freedom` is the one command a reviewer runs; if the lock check is not in it,
        the gate exists on paper only."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "synthverify.cli", "freedom"],
            capture_output=True, text=True, cwd=REPO_ROOT, check=False, encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Dependency lock:" in result.stdout
