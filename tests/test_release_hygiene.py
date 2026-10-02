"""Release hygiene: the checkout a stranger clones must install and describe itself correctly.

These are not unit tests of behaviour. They pin the four shapes that made a clean clone of this
repository fail its own test suite - an installer that skipped a declared extra, a venv that
inherited site-packages, prose that assumed the author's machine - and each one is cheap to
re-introduce because nothing in the product breaks when they come back. Only the stranger's
experience breaks, and a stranger does not run this suite: they run `make setup && make test`
and stop.

`docs/goal-spec.md` calls this the difference between a claim and a gate. FC-1 grades *installed
metadata*, so an environment with a hole in it is a gate that cannot see the hole; these tests
make the hole visible to anyone who runs the suite at all.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import new_database_url

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The prefix `.github/workflows/ci.yml` counts Windows skips by. Changing it changes the count,
#: which is the point: the allowance is earned by naming the skip, not by widening the ceiling.
MODE_BITS_SKIP = "POSIX mode bits: st_mode on Windows is the CRT read-only flag, not a permission set"
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
DOCTOR = REPO_ROOT / "scripts" / "doctor.py"


def _doctor():
    """Load `scripts/doctor.py` as a module - it is a script, so it is not importable by name."""
    spec = importlib.util.spec_from_file_location("doctor_under_test", DOCTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _markdown_link_targets(text: str) -> list[str]:
    """Every link target a renderer would follow, with code spans and fenced blocks blanked out.

    A guard against broken links has to be able to *describe* a broken link, and this repository's
    CHANGELOG does so in backticks - so anything inside a code span is treated as prose, not as a
    link, exactly as a Markdown renderer would.
    """
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        kept.append("" if in_fence else re.sub(r"`[^`]*`", " ", line))
    return re.findall(r"\]\(([^)\s]+)", "\n".join(kept))


class TestTheProjectMetadataIsStructurallySound:
    """A TOML table header ends its parent's key list, and nothing tells you it happened.

    Moving `[project.urls]` above `dependencies` parses cleanly, installs cleanly, and reports
    `core=0` to the closure walk - which then calls all 28 real pins stale. The gate caught that in
    seconds; this catches it in the suite, for an edit that never reaches a `make` target.
    """

    def test_the_core_requirement_list_is_attached_to_the_project_table(self) -> None:
        project = PYPROJECT["project"]
        assert "dependencies" in project, "`dependencies` fell out of [project] - see the comment at [project.urls]"
        assert len(project["dependencies"]) == 11, "FC-1's core root count is documented as 11 requirements"
        assert "dependencies" not in project.get("urls", {}), "`dependencies` landed inside [project.urls]"

    def test_every_declared_group_names_at_least_one_requirement(self) -> None:
        naming = _doctor().requirement_name
        groups = {
            "core": PYPROJECT["project"]["dependencies"],
            **PYPROJECT["project"]["optional-dependencies"],
            "build-system": PYPROJECT["build-system"]["requires"],
        }
        for group, specs in groups.items():
            assert specs, f"{group} declares nothing, so the gates grade nothing"
            assert all(naming(spec) for spec in specs), f"{group} has a requirement line with no name"

    def test_the_declared_root_count_matches_what_the_gates_print(self) -> None:
        """`make licenses` reports "52 packages from 22 declared roots", and README quotes the 22.

        T49 (REQ-IDAM-1 / AC-IDAM-1) adds `PyJWT` as a new declared root (via the `jwt` extra and
        the `dev` extra, so the OIDC tests run offline in CI); `make typecheck` needs `types-PyYAML`
        for the alert-rules scanner to type-check. The full 22-root closure adds `PyJWT` +
        `cryptography` + `cffi` + `pycparser` + `types-PyYAML` to the 47 the pre-T49 scan showed.
        """
        naming = _doctor().requirement_name
        groups = {
            "core": PYPROJECT["project"]["dependencies"],
            **PYPROJECT["project"]["optional-dependencies"],
            "build-system": PYPROJECT["build-system"]["requires"],
        }
        declared = {naming(spec).lower().replace("_", "-") for specs in groups.values() for spec in specs}
        assert len(declared) == 22, (
            "the declared-root count is quoted in README.md, docs/goal-spec.md and `make licenses`; "
            "change it here only with those sentences"
        )

    def test_typing_classifier_is_backed_by_a_pep_561_marker(self) -> None:
        """"Typing :: Typed" is a claim about the installed artifact, not about the source tree."""
        classifiers = PYPROJECT["project"]["classifiers"]
        typed = any(classifier.startswith("Typing ::") for classifier in classifiers)
        assert typed == (REPO_ROOT / "synthverify" / "py.typed").is_file(), (
            "the classifier and the marker must agree, in either direction"
        )
        if typed:
            assert "py.typed" in PYPROJECT["tool"]["setuptools"]["package-data"]["synthverify"], (
                "setuptools ships the package-data list, not the directory's contents"
            )


class TestTheInstallerCoversEveryDeclaredRoot:
    """`make setup` must install what the gates grade.

    This is the regression that produced fourteen red tests on a clean clone: FC-1 and the T39
    lock check both *fail closed* on a declared root whose metadata they cannot read, so
    installing only `.[dev]` turned a correct checkout into a failing one, and CI hid it because
    CI installs all three extras.
    """

    def test_the_makefile_installs_every_optional_extra(self) -> None:
        declared = set(PYPROJECT["project"]["optional-dependencies"])
        recipe = re.search(r'^ALL_EXTRAS\s*:=\s*(\S+)\s*$', MAKEFILE, re.MULTILINE)
        assert recipe, "make setup must name its extras through ALL_EXTRAS, not inline"
        installed = set(recipe.group(1).split(","))
        assert installed == declared, (
            f"pyproject.toml declares extras {sorted(declared)} but `make setup` installs "
            f"{sorted(installed)}; a root that is not installed cannot be graded by FC-1, which is "
            "a violation rather than a pass"
        )

    def test_the_setup_recipe_uses_the_variable_and_the_lock(self) -> None:
        setup = re.search(r"^setup:.*?(?=\n\w[\w-]*:|\Z)", MAKEFILE, re.MULTILINE | re.DOTALL)
        assert setup
        body = setup.group(0)
        assert '-c docker/requirements-lock.txt' in body, "setup must install through the lock"
        assert '".[$(ALL_EXTRAS)]"' in body, "setup must install the ALL_EXTRAS set, not a subset"
        assert "--system-site-packages" not in body, (
            "a venv that inherits site-packages lets an un-locked package satisfy a pin locally "
            "and fail on a clean machine"
        )

    def test_the_python_floor_is_one_claim_and_it_is_importable(self) -> None:
        """`requires-python` was wrong: it said 3.10 while `synthverify/db.py` imports
        `enum.StrEnum`, which arrived in 3.11. `pip install` on 3.10 succeeds - the marker is
        satisfied by the *declared* floor, not by what the code imports - and the first
        `import synthverify` raises, so the failure lands on the user who trusted the metadata.
        Measured on `python:3.10-slim`: ``ImportError: cannot import name 'StrEnum' from 'enum'``.

        The floor is therefore pinned four ways: the value itself, the interpreter probe `make
        setup` runs, the prose a reader decides from, and the classifiers, which are where an
        unsupported version advertises itself to a resolver.
        """
        match = re.fullmatch(r">=\s*(3\.\d+)", PYPROJECT["project"]["requires-python"])
        assert match, f"requires-python must be a plain floor: {PYPROJECT['project']['requires-python']!r}"
        floor = tuple(int(p) for p in match.group(1).split("."))
        assert floor == (3, 11), "StrEnum sets the floor at 3.11; changing this needs a source reason"

        probe = re.search(r"sys\.version_info < \((\d+), (\d+)\)", MAKEFILE)
        assert probe, "make setup must probe for the floor rather than assume the reader's python3"
        assert (int(probe.group(1)), int(probe.group(2))) == floor, "the Makefile probe and the metadata disagree"
        assert "python3.10" not in MAKEFILE, "the probe list still offers an interpreter below the floor"

        install = (REPO_ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        for source, name in ((README, "README.md"), (install, "docs/INSTALL.md"), (MAKEFILE, "Makefile")):
            assert not re.search(r">=\s*\*?\*?\s*3\.10", source), f"{name} still offers 3.10 as supported"

        for classifier in PYPROJECT["project"]["classifiers"]:
            m = re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", classifier)
            if m:
                assert tuple(int(p) for p in m.group(1).split(".")) >= floor, (
                    f"{classifier} claims a version below requires-python"
                )

    def test_every_advertised_interpreter_is_run_by_ci_on_both_laptop_platforms(self) -> None:
        """`Programming Language :: Python :: 3.12` is a promise to a resolver, and until this
        matrix carried 3.12 the only 3.12 evidence in the repository was one author's container.

        The classifiers are the claim and `portability` is the gate; reading one without the other
        is how a supported version ends up supported in metadata only. A version added to the
        classifiers without being added here fails, and so does one dropped from the matrix while
        the metadata still advertises it.
        """
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = ci.split("  portability:", 1)[1].split("\n  freedom:", 1)[0]
        advertised = {
            m.group(1)
            for c in PYPROJECT["project"]["classifiers"]
            if (m := re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", c))
        }
        run = set(re.search(r'python-version:\s*\[([^\]]+)\]', job).group(1).replace('"', "").split(","))
        assert {v.strip() for v in run} == advertised, (
            f"classifiers advertise {sorted(advertised)} but the portability matrix runs "
            f"{sorted(v.strip() for v in run)}"
        )
        for platform in ("macos-latest", "windows-latest"):
            assert platform in job, f"{platform} left the portability matrix"

    def test_the_docker_image_can_reach_its_own_documented_backends(self) -> None:
        """The compose file tells users to set `SV_RATE_LIMIT_BACKEND=valkey`; the image must serve it.

        `ValkeyRateLimiter.__init__` imports its client lazily by FC-1 design, so an image without
        the extra raises at limiter construction - a working README instruction the artifact
        cannot honour.
        """
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        extras = set(re.search(r'pip install -c requirements-lock\.txt "\.\[([\w,]+)\]"', dockerfile).group(1).split(","))
        assert "vision" in extras
        assert "valkey" in extras, (
            "docker-compose.yml names the shared bucket as the scale-out path, so the shipped "
            "image needs valkey-py even though `dev` stays out of it"
        )
        assert "dev" not in extras, "the image must not carry the test toolchain"


class TestNoClaimDependsOnTheAuthorsMachine:
    """Nothing that ships may name an absolute path or an OS prompt that only exists here.

    "Measured on this machine" stays welcome - it is the honest way to report a timing - but a
    reader who does not have Homebrew on `/opt/homebrew`, or who has never seen an Xcode license
    prompt, cannot follow an instruction written in those terms.
    """

    FORBIDDEN = (
        "/opt/homebrew",
        "/Users/",
        "Xcode",
    )

    def _shipped_text_files(self) -> list[Path]:
        candidates = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "Makefile",
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "SECURITY.md",
            REPO_ROOT / "CONTRIBUTING.md",
            REPO_ROOT / "CHANGELOG.md",
            REPO_ROOT / "CODE_OF_CONDUCT.md",
            *sorted((REPO_ROOT / "docs").glob("*.md")),
            *sorted((REPO_ROOT / ".github").rglob("*.yml")),
            *sorted((REPO_ROOT / ".github").rglob("*.md")),
            *sorted((REPO_ROOT / "docker").glob("*.yml")),
            *sorted((REPO_ROOT / "docker").glob("Dockerfile*")),
        ]
        return [path for path in candidates if path.is_file()]

    def test_shipped_files_never_name_this_machine(self) -> None:
        offenders: list[str] = []
        for path in self._shipped_text_files():
            for token in self.FORBIDDEN:
                for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    if token in line:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {token}")
        assert not offenders, "these read as instructions to a reader who does not have this box: " + "; ".join(offenders)

    def test_the_quickstart_does_not_assume_an_existing_venv(self) -> None:
        """`./.venv/bin/python ...` is only a first command if something created `.venv` first."""
        quickstart = README.split("## 4.", 1)[1].split("\n## ", 1)[0]
        assert "already installed" not in quickstart.lower()
        assert re.search(r"make setup", quickstart), "the quickstart must create the environment it uses"

    def test_no_shipped_document_links_to_a_path_the_reader_does_not_have(self) -> None:
        """`SECURITY.md` carried `](architecture.md)` and `](../README.md)`: both resolve on the
        tree their author was editing, and 404 from the repository root where a reader is.

        Relative links are the only route a cloned `README.md` has to the rest of the docs, and no
        other test resolves them - GitHub renders the page without complaining about a target that
        is not there. Anchors (`#section`) are not checked, because a wrong anchor still lands on
        the right page.
        """
        unresolvable: list[str] = []
        for path in self._shipped_text_files():
            if path.suffix != ".md":
                continue
            for target in _markdown_link_targets(path.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:", "#", "/")):
                    continue
                file_part = target.split("#", 1)[0]
                if file_part and not (path.parent / file_part).resolve().exists():
                    unresolvable.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
        assert not unresolvable, (
            "these links are written against someone's working tree rather than the repository: "
            + "; ".join(unresolvable)
        )


class TestDoctorDescribesABrokenEnvironment:
    """`scripts/doctor.py` is the difference between an actionable error and fourteen traces."""

    def test_doctor_passes_in_the_test_environment(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(DOCTOR), "--quiet"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False, encoding="utf-8",
        )
        assert proc.returncode == 0, f"doctor failed in the environment running the suite:\n{proc.stdout}{proc.stderr}"

    def test_a_missing_extra_is_named_with_the_command_that_fixes_it(self) -> None:
        doctor = _doctor()
        report = doctor.Report(quiet=False)
        original = doctor.installed_names
        doctor.installed_names = lambda: set()
        try:
            doctor.check_extras_installed(report)
        finally:
            doctor.installed_names = original
        failures = [(subject, detail) for status, subject, detail in report.rows if status == doctor.FAIL]
        assert failures, "an environment with nothing installed must not be reported as ready"
        every_group = {subject for subject, _ in failures}
        assert {"core not installed", "vision not installed", "valkey not installed"} <= every_group
        for subject, detail in failures:
            assert "make setup" in detail, f"{subject} names the problem but not the fix"

    def test_the_three_environment_shapes_are_graded_differently(self, tmp_path) -> None:
        """A venv that inherits is broken; an interpreter with no venv is a container; both are visible.

        The `check_self_contained` signature takes the interpreter's shape as arguments so each
        branch is driven rather than observed - including the one this repository actually shipped
        once, where the environment was green here and red everywhere else.
        """
        doctor = _doctor()
        rows: dict[str, dict[str, str]] = {}
        failures: dict[str, list[str]] = {}
        for label, kwargs in {
            "container": {"prefix": Path("/usr/local"), "base": Path("/usr/local"), "pyvenv_cfg": ""},
            "inheriting-venv": {
                "prefix": tmp_path / ".venv",
                "base": Path("/usr"),
                "pyvenv_cfg": "include-system-site-packages = true\n",
            },
            "clean-venv": {"prefix": tmp_path / ".venv", "base": Path("/usr"), "pyvenv_cfg": "include-system-site-packages = false\n"},
        }.items():
            report = doctor.Report(quiet=False)
            doctor.check_self_contained(report, **kwargs)
            rows[label] = {subject: status for status, subject, _ in report.rows}
            failures[label] = [subject for status, subject, _ in report.rows if status == doctor.FAIL]
        assert failures["container"] == [], "a container installs with no venv, and CI does too"
        assert failures["clean-venv"] == []
        assert failures["inheriting-venv"] == ["site-packages isolation"]
        assert rows["inheriting-venv"]["site-packages isolation"] == doctor.FAIL
        assert rows["clean-venv"]["site-packages isolation"] == doctor.OK
        assert rows["container"]["virtualenv"] == doctor.WARN

    def test_doctor_agrees_with_the_venv_running_the_suite(self) -> None:
        """Whatever shape this run happens to be in, doctor must classify it correctly."""
        doctor = _doctor()
        report = doctor.Report(quiet=False)
        doctor.check_self_contained(report)
        statuses = {subject: status for status, subject, _ in report.rows}
        inherits = bool(
            re.search(r"include-system-site-packages\s*=\s*true", (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8"))
            if (Path(sys.prefix) / "pyvenv.cfg").is_file()
            else False
        )
        if sys.prefix == sys.base_prefix:
            assert statuses["virtualenv"] == doctor.WARN
        else:
            assert statuses["site-packages isolation"] == (doctor.FAIL if inherits else doctor.OK)

    def test_requirement_name_handles_every_shape_pyproject_uses(self) -> None:
        doctor = _doctor()
        naming = doctor.requirement_name
        assert naming("valkey>=6.0,<7") == "valkey"
        assert naming('uvicorn[standard]>=0.27') == "uvicorn"
        assert naming("async-timeout>=4.0.3; python_version < '3.11.3'") == "async-timeout"
        assert naming("opencv-python>=4.8") == "opencv-python"


class TestTheFirstBootCredential:
    """`data/bootstrap_admin_key.txt` is a full admin credential sitting in a working directory.

    What this pins is the end state: mode `0600`, exactly, whatever the process umask was. The
    window that `write_text()` followed by `chmod()` opened - a moment where the secret exists at
    the umask's default, 0644 on a stock host - is closed by supplying the mode to `open()` itself,
    which no assertion can observe after the fact; the mutation this pair *does* catch is a change
    that leaves group or other bits set, or that writes a file whose contents are not the key the
    database was given.
    """

    def _mint(self, tmp_path, monkeypatch, umask: int) -> tuple[object, Path]:
        import stat

        from synthverify import app as app_module
        from synthverify.config import Settings
        from synthverify.db import Database

        url, teardown = new_database_url(tmp_path, "bootstrap")
        monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(app_module, "get_settings", lambda: Settings(environment="development", bootstrap_admin_key=None))
        db = Database(url)
        db.create_all()
        previous = os.umask(umask)
        try:
            secret = app_module._bootstrap_admin_key(db)
        finally:
            os.umask(previous)
            teardown()
        assert secret, "a database with no admin key must produce one"
        path = tmp_path / "data" / "bootstrap_admin_key.txt"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, f"umask {oct(umask)} leaked into the key file's mode"
        assert path.read_text(encoding="utf-8").strip() == secret
        return secret, path

    # The subject of these two cases is a POSIX permission triple. Windows has no referent for it:
    # `st_mode` there reports the CRT read-only flag expanded to 0o666 or 0o444 for every file, so
    # `0o600` is unachievable *and* `S_IWGRP`/`S_IWOTH` are unassertable, and the NTFS ACL that would
    # carry the same meaning is not what `os.open(..., 0o600)` writes. Skipped rather than loosened,
    # because a weakened assertion here would read as a pass on the platforms where it can be proven.
    # `.github/workflows/ci.yml` counts this skip by its prefix and fails if the count moves.
    @pytest.mark.skipif(os.name == "nt", reason=MODE_BITS_SKIP)
    def test_the_key_file_is_owner_only_under_a_permissive_umask(self, tmp_path, monkeypatch) -> None:
        self._mint(tmp_path, monkeypatch, 0o000)

    @pytest.mark.skipif(os.name == "nt", reason=MODE_BITS_SKIP)
    def test_the_key_file_is_owner_only_under_a_restricted_umask(self, tmp_path, monkeypatch) -> None:
        # A umask that already excludes group/other must not be the only case that works: the mode
        # is asserted, not inherited from the environment.
        self._mint(tmp_path, monkeypatch, 0o077)

    def test_exactly_two_cases_sit_out_when_mode_bits_do_not_exist(self) -> None:
        """The Windows ceiling in `.github/workflows/ci.yml` is 21 + 2, and these are those 2.

        Counted from the marks rather than asserted in prose, so the allowance cannot grow by
        decorating a third case, and cannot be quietly unused: on a platform where mode bits do
        exist, the same marks must be inactive.
        """
        marked = []
        for name in sorted(n for n in dir(type(self)) if n.startswith("test_")):
            for marker in getattr(getattr(type(self), name), "pytestmark", []):
                if marker.name != "skipif":
                    continue
                if str(marker.kwargs.get("reason", "")).startswith("POSIX mode bits:"):
                    marked.append((name, bool(marker.kwargs.get("condition"))))
        assert len(marked) == 2, marked
        expected_active = os.name == "nt"
        assert {active for _, active in marked} == {expected_active}, (
            f"the mode-bit marks are {marked} on a platform where st_mode is {'POSIX' if not expected_active else 'CRT'}"
        )

    def test_a_second_boot_mints_nothing_because_an_admin_exists(self, tmp_path, monkeypatch) -> None:
        from synthverify import app as app_module
        from synthverify.config import Settings
        from synthverify.db import Database

        url, teardown = new_database_url(tmp_path, "bootstrap-twice")
        monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(app_module, "get_settings", lambda: Settings(environment="development", bootstrap_admin_key=None))
        db = Database(url)
        db.create_all()
        try:
            first = app_module._bootstrap_admin_key(db)
            assert first
            assert app_module._bootstrap_admin_key(db) is None, (
                "a second boot minted another admin key: the first one is now unrevoked and unknown"
            )
        finally:
            teardown()

    def test_a_supplied_key_is_never_written_to_the_file(self, tmp_path, monkeypatch) -> None:
        """`SV_BOOTSTRAP_ADMIN_KEY` exists so operators can avoid exactly this artifact."""
        from synthverify import app as app_module
        from synthverify.config import Settings
        from synthverify.db import Database

        supplied = "sv_live_" + "0" * 32
        url, teardown = new_database_url(tmp_path, "bootstrap-env")
        monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(app_module, "get_settings", lambda: Settings(environment="production", bootstrap_admin_key=supplied))
        db = Database(url)
        db.create_all()
        try:
            assert app_module._bootstrap_admin_key(db) == supplied
            assert not (tmp_path / "data" / "bootstrap_admin_key.txt").exists(), (
                "an operator-supplied credential was copied onto disk, widening its exposure"
            )
        finally:
            teardown()


# --------------------------------------------------------------------- text I/O encoding census

#: Sources that ship to a stranger's machine. Everything under these three roots runs on at least
#: one of the three OS families the CI matrix covers, so a locale assumption is a defect here.
TEXT_IO_ROOTS = ("synthverify", "tests", "scripts")

#: `(namespace, call)` pairs that are **not** a Python text stream: a raw file descriptor, a binary
#: container parser, or an opener whose own default mode is already `w+b`. Naming them is what keeps
#: the census from shouting about calls that were never locale-dependent.
NATIVE_OPENERS = {
    ("os", "open"),  # returns a descriptor; there is no codec layer to configure
    ("io", "open"),  # takes encoding positionally
    ("codecs", "open"),
    ("wave", "open"),  # binary audio container
    ("Image", "open"),  # PIL: bytes in, pixels out
    ("PIL", "open"),
    ("gzip", "open"),
    ("bz2", "open"),
    ("lzma", "open"),
    ("zipfile", "open"),
    ("tarfile", "open"),
}

#: `subprocess` entry points that return decoded stdout when asked for text.
SPAWNERS = {"run", "Popen", "check_output", "check_call", "call"}


def _callee(node: ast.Call) -> tuple[bool, str | None, str]:
    """`(builtin_shape, namespace, name)` - `builtin_shape` means the mode argument is second."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return True, None, fn.id
    if isinstance(fn, ast.Attribute):
        receiver = fn.value
        return False, receiver.id if isinstance(receiver, ast.Name) else None, fn.attr
    return False, "", ""


def _str_constant(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _text_mode_spawn(node: ast.AST) -> bool:
    """True for `subprocess.<spawn>(..., text=True, ...)` with a literal True."""
    if not isinstance(node, ast.Call):
        return False
    builtin, namespace, name = _callee(node)
    if name not in SPAWNERS or namespace != "subprocess" or builtin:
        return False
    for keyword in node.keywords:
        if keyword.arg in {"text", "universal_newlines"}:
            return isinstance(keyword.value, ast.Constant) and keyword.value.value is True
    return False


def unencoded_text_io(root: Path) -> tuple[list[str], int]:
    """Every text-mode I/O call under *root* that leaves its encoding to the OS locale.

    Returns `(findings, files_parsed)`. The second number is the dispatch proof: a census that
    parsed nothing also found nothing, and "found nothing" is worth only as much as the parse that
    produced it.
    """
    findings: list[str] = []
    parsed = 0
    for part in TEXT_IO_ROOTS:
        for module in sorted((root / part).rglob("*.py")):
            if "__pycache__" in module.parts:
                continue
            parsed += 1
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                builtin, namespace, name = _callee(node)
                keywords = {k.arg: k for k in node.keywords if k.arg}
                if "encoding" in keywords:
                    continue
                where = f"{module.relative_to(root)}:{node.lineno}"

                if name in {"read_text", "write_text"}:
                    findings.append(f"{where}  {name}() on a path - the OS codec reads/writes it")
                    continue

                if name == "open" or (namespace, name) == ("os", "fdopen"):
                    if (namespace, name) in NATIVE_OPENERS:
                        continue
                    # `open(file, mode)` and `os.fdopen(fd, mode)` put the mode second;
                    # `Path.open(mode)` puts it first.
                    index = 1 if (builtin or namespace == "os") else 0
                    mode_node = node.args[index] if len(node.args) > index else keywords.get("mode")
                    mode = "r" if mode_node is None else _str_constant(mode_node)
                    if mode is None:
                        # A computed mode is how this class hides, so it is reported rather than
                        # skipped: the fix is to name the mode, not to widen an exclusion.
                        findings.append(f"{where}  open() with a non-literal mode - cannot be cleared")
                    elif "b" not in mode:
                        findings.append(f"{where}  open(..., mode={mode!r}) - the OS codec reads/writes it")
                    continue

                if _text_mode_spawn(node):
                    keyword = "text" if "text" in keywords else "universal_newlines"
                    findings.append(
                        f"{where}  subprocess.{name}({keyword}=True) without encoding= - "
                        "the parent decodes in the OS codec"
                    )
    return sorted(findings), parsed


class TestTextIoNamesItsEncoding:
    """Windows is the platform that turns an unnamed encoding into a bug rather than a style choice.

    A text-mode read with no `encoding=` uses the OS locale, and on Windows that is cp1252 - which
    cannot decode the bytes of this repository's own documentation. The hosted `windows-latest` leg
    reached `tests/test_release_hygiene.py`, read `README.md` with `read_text()`, and died during
    collection on `UnicodeDecodeError: 'charmap' codec can't decode byte 0x90 in position 86860`.

    The 120 sites that run is the fix; this census is what keeps the class from coming back. No
    runtime test can do that on the machine that develops it: macOS silently coerces the `C` locale
    to UTF-8, so `LANG=C pytest` passes here while a Windows runner fails.
    """

    def test_the_census_finds_nothing_left_to_name(self) -> None:
        findings, parsed = unencoded_text_io(REPO_ROOT)
        assert parsed >= 126, f"the census parsed {parsed} files; it has to read the tree to prove anything"
        assert findings == [], "text I/O that inherits the OS locale:\n" + "\n".join(findings)

    def test_the_census_reports_every_shape_the_windows_run_died_on(self, tmp_path) -> None:
        """A gate that cannot go red is not a gate: each clause is planted and must be caught."""
        sample = tmp_path / "synthverify" / "planted.py"
        sample.parent.mkdir(parents=True)
        sample.write_text(
            '''"""A file in the shapes the real tree used to have."""
import subprocess
from pathlib import Path


def read(path: Path) -> str:
    return path.read_text()


def append(path: Path, text: str) -> None:
    path.write_text(text)


def rewrite(path: Path) -> str:
    with path.open("w") as handle:
        handle.write("x")
    with open(path, "r") as handle:
        return handle.read()


def gate(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def legacy(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, universal_newlines=True).stdout
''',
            encoding="utf-8",
        )
        findings, parsed = unencoded_text_io(tmp_path)
        assert parsed == 1, "the planted file was never parsed, so this proves nothing"
        # Named one by one: six findings could still be six findings with a clause missing.
        assert sum("read_text" in f for f in findings) == 1, findings
        assert sum("write_text" in f for f in findings) == 1, findings
        assert sum("mode='w'" in f for f in findings) == 1, findings
        assert sum("mode='r'" in f for f in findings) == 1, findings
        assert sum("text=True" in f for f in findings) == 1, findings
        assert sum("universal_newlines" in f for f in findings) == 1, findings
        assert len(findings) == 6, findings

    def test_a_mode_the_census_cannot_read_is_reported_not_skipped(self, tmp_path) -> None:
        """Silence has to cost something, or a dynamic mode becomes a way to hide in plain sight."""
        sample = tmp_path / "synthverify" / "dynamic.py"
        sample.parent.mkdir(parents=True)
        sample.write_text(
            "import sys\n\n\ndef read(path, how):\n    return open(path, how).read()\n",
            encoding="utf-8",
        )
        findings, parsed = unencoded_text_io(tmp_path)
        assert parsed == 1
        assert len(findings) == 1 and "non-literal mode" in findings[0], findings

    def test_the_census_stays_quiet_about_the_opens_that_are_not_text(self, tmp_path) -> None:
        """The exemption table is proven, not assumed - otherwise the census shrinks to nothing."""
        sample = tmp_path / "synthverify" / "exempt.py"
        sample.parent.mkdir(parents=True)
        sample.write_text(
            '''"""Everything here is already codec-free, and must stay out of the report."""
import os
import subprocess
import wave
from pathlib import Path

from PIL import Image


def bytes_in(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read()


def appended(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b"x")


def descriptor(path: Path) -> int:
    return os.open(path, os.O_RDONLY)


def pixels(path: Path) -> Image.Image:
    return Image.open(path)


def frames(handle) -> int:
    with wave.open(handle, "rb") as wav:
        return wav.getnframes()


def named(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def child(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8").stdout


def binary(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, capture_output=True).stdout
''',
            encoding="utf-8",
        )
        findings, parsed = unencoded_text_io(tmp_path)
        assert parsed == 1, "the exempt file was never parsed, so this proves nothing"
        assert findings == [], findings

    def test_the_census_does_not_invent_findings_for_helpers_named_run(self, tmp_path) -> None:
        """`def run(...)` is a local helper in two scripts; only `subprocess.run` returns text."""
        sample = tmp_path / "scripts" / "local_run.py"
        sample.parent.mkdir(parents=True)
        sample.write_text(
            "def run(cmd):\n    return cmd\n\n\nrun(['ls'], text=True)\n",
            encoding="utf-8",
        )
        findings, parsed = unencoded_text_io(tmp_path)
        assert parsed == 1
        assert findings == [], findings

    def test_every_script_that_captures_child_text_names_the_childs_codec(self) -> None:
        """The parent's `encoding=` is half of it; a Python child writes in the OS codec by default.

        A child started from a Windows runner encodes its stdout as cp1252 unless it is put in UTF-8
        mode, so naming `encoding="utf-8"` on the reading end alone would only move the crash from
        the parent's decoder into the child's encoder. `os.environ.setdefault` at module scope covers
        every child a script starts, including the ones built as `{**os.environ, ...}`. The rule is
        total on purpose - a file that only spawns `docker` carries the line too - because an
        exception here would have to be re-argued every time a spawn is added. The suite's children
        are covered once, in `tests/conftest.py`, which every test file inherits.
        """
        missing = []
        spawning = 0
        for module in sorted((REPO_ROOT / "scripts").rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            if not any(_text_mode_spawn(node) for node in ast.walk(tree)):
                continue
            spawning += 1
            if "PYTHONUTF8" not in module.read_text(encoding="utf-8"):
                missing.append(str(module.relative_to(REPO_ROOT)))
        assert spawning == 13, f"expected the 13 spawn-capturing scripts measured today, saw {spawning}"
        assert missing == [], "children can emit cp1252 into a UTF-8 reader: " + ", ".join(missing)
        assert "PYTHONUTF8" in (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"), (
            "conftest.py is the single place the suite's child processes get their codec from"
        )
