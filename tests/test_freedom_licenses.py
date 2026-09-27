"""FC-1: the permissive-only dependency licence gate (spec §2.1).

Two halves. The classifier must reject anything it cannot vouch for, and the
real declared dependency set of this repository must pass. The second half is
what CI runs; the first half is what proves the gate is not vacuous.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from synthverify.cli import main as cli_main
from synthverify.compliance.licenses import (
    FORBIDDEN_LICENSES,
    LicenseFinding,
    LicenseStatus,
    Requirement,
    ScanReport,
    _license_files,
    _normalise,
    classify_expression,
    declared_requirements,
    dependency_closure,
    marker_applies,
    parse_requirement,
    resolve_dist_license,
    scan_declared_dependencies,
    sniff_licence_text,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeDist:
    """Minimal stand-in for ``importlib.metadata.Distribution``.

    Only the surface the scanner touches is implemented, and headers keep their
    real (dashed, repeated) shape - ``Requires-Dist`` is a multi-value header.
    """

    def __init__(
        self,
        name: str,
        version: str = "1.0",
        files: list[Any] | None = None,
        headers: dict[str, str | list[str]] | None = None,
    ):
        self.metadata = Message()
        self.metadata["Name"] = name
        for header, value in (headers or {}).items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                self.metadata[header] = item
        self.version = version
        self.files = files


class FakeEntry:
    """A ``dist.files`` element: path parts plus bytes, nothing else."""

    def __init__(self, parts: tuple[str, ...], content: str):
        self.parts = parts
        self._content = content.encode("utf-8")

    def read_bytes(self) -> bytes:
        return self._content


def req(spec: str) -> Requirement:
    parsed = parse_requirement(spec)
    assert parsed is not None
    return parsed


# ------------------------------------------------------------------ classifier


class TestClassifyExpression:
    @pytest.mark.parametrize(
        "expression",
        [
            "MIT",
            "Apache-2.0",
            "BSD-3-Clause",
            "BSD-2-Clause",
            "ISC",
            "PSF-2.0",
            "MPL-2.0",
            "MIT-CMU",
            "0BSD",
            "Unlicense",
        ],
    )
    def test_allowlist_accepts_permissive_ids(self, expression: str):
        status, canonical = classify_expression(expression)
        assert status is LicenseStatus.ALLOWED
        assert canonical == expression

    @pytest.mark.parametrize("expression", sorted(FORBIDDEN_LICENSES))
    def test_every_forbidden_id_is_rejected(self, expression: str):
        assert classify_expression(expression)[0] is LicenseStatus.FORBIDDEN

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("mit", "MIT"),
            ("MIT License", "MIT"),
            ("Apache 2.0", "Apache-2.0"),
            ("apache software license", "Apache-2.0"),
            ("BSD License", "BSD-3-Clause"),
            ("New BSD License", "BSD-3-Clause"),
            ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
            ("Python Software Foundation License", "PSF-2.0"),
            ("ISC License", "ISC"),
        ],
    )
    def test_historical_spellings_normalise(self, expression: str, expected: str):
        status, canonical = classify_expression(expression)
        assert status is LicenseStatus.ALLOWED
        assert canonical == expected

    @pytest.mark.parametrize(
        "expression",
        [
            "GNU General Public License v2 or later (GPLv2+)",
            "GNU General Public License v3 (GPLv3)",
            "GNU Affero General Public License v3 (AGPLv3)",
            "GPL-3.0-or-later",
            "SSPL-1.0",
            "BUSL-1.1",
            "Elastic-2.0",
            "CC-BY-NC-4.0",
        ],
    )
    def test_copyleft_and_source_available_are_rejected(self, expression: str):
        assert classify_expression(expression)[0] is LicenseStatus.FORBIDDEN

    @pytest.mark.parametrize(
        ("expression", "canonical"),
        [
            ("GNU Lesser General Public License v2 or later (LGPLv2+)", "LGPL-2.1-or-later"),
            ("LGPL-3.0-or-later", "LGPL-3.0-or-later"),
        ],
    )
    def test_lgpl_is_conditional_not_silently_allowed(self, expression: str, canonical: str):
        """FC-1 admits LGPL only under the dynamic-linking caveat, so it is
        surfaced separately instead of passing as plain MIT-like."""
        status, resolved = classify_expression(expression)
        assert status is LicenseStatus.CONDITIONAL
        assert resolved == canonical

    def test_gpl_is_never_rescued_by_loose_prefix_matching(self):
        """The dangerous confusion: 'gpl' must not resolve to 'LGPL-*'."""
        assert classify_expression("GPL")[0] is LicenseStatus.FORBIDDEN
        assert classify_expression("gpl-2.0")[0] is LicenseStatus.FORBIDDEN
        assert classify_expression("agpl")[0] is LicenseStatus.FORBIDDEN

    def test_or_expression_can_be_satisfied_by_one_permissive_branch(self):
        status, canonical = classify_expression("Apache-2.0 OR GPL-2.0-or-later")
        assert status is LicenseStatus.ALLOWED
        assert canonical == "Apache-2.0"

    def test_and_expression_requires_every_branch_to_pass(self):
        assert classify_expression("MIT AND GPL-3.0-or-later")[0] is LicenseStatus.FORBIDDEN

    def test_parentheses_are_respected(self):
        """``A AND (B OR C)`` does not grant a free choice of A; a naive string
        split would read it as a flat OR and wrongly pass a GPL-bound package."""
        assert classify_expression("MIT AND (GPL-3.0-or-later OR Apache-2.0)")[0] is LicenseStatus.ALLOWED
        assert classify_expression("GPL-2.0-only AND (MIT OR Apache-2.0)")[0] is LicenseStatus.FORBIDDEN

    @pytest.mark.parametrize("expression", ["", "unknown", "None", "Other/Proprietary License"])
    def test_non_licences_are_unknown_not_allowed(self, expression: str):
        assert classify_expression(expression)[0] is LicenseStatus.UNKNOWN

    def test_unrecognised_id_is_unknown(self):
        assert classify_expression("Do-The-Willingness-Of-Horse-1.0")[0] is LicenseStatus.UNKNOWN


class TestLicenceTextSniffing:
    MIT_TEXT = "Permission is hereby granted, free of charge, to any person obtaining a copy"
    BSD_TEXT = "Redistribution and use in source and binary forms, with or without modification"
    APACHE_TEXT = "Apache License\n                           Version 2.0, January 2004"
    AGPL_TEXT = "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007"
    GPL_TEXT = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007"
    NC_TEXT = "Creative Commons Attribution-NonCommercial 4.0"

    def test_permissive_bodies_recognised(self):
        assert sniff_licence_text(self.MIT_TEXT)[0] is LicenseStatus.ALLOWED
        assert sniff_licence_text(self.BSD_TEXT)[0] is LicenseStatus.ALLOWED
        assert sniff_licence_text(self.APACHE_TEXT)[0] is LicenseStatus.ALLOWED

    def test_affero_beats_the_generic_gpl_phrase(self):
        assert sniff_licence_text(self.AGPL_TEXT)[1] == "AGPL-3.0-or-later"
        assert sniff_licence_text(self.GPL_TEXT)[0] is LicenseStatus.FORBIDDEN

    def test_non_commercial_body_is_forbidden(self):
        assert sniff_licence_text(self.NC_TEXT)[0] is LicenseStatus.FORBIDDEN

    def test_unrecognised_body_is_unknown(self):
        assert sniff_licence_text("thou shalt not pass go")[0] is LicenseStatus.UNKNOWN


class TestResolutionOrder:
    def test_pep639_expression_wins(self):
        dist = FakeDist("x", headers={"License-Expression": "MIT", "License": "GNU GPL v3"})
        status, canonical, evidence, _ = resolve_dist_license(dist)
        assert (status, canonical, evidence) == (LicenseStatus.ALLOWED, "MIT", "License-Expression")

    def test_classifier_used_when_no_expression(self):
        dist = FakeDist("x", headers={"Classifier": "License :: OSI Approved :: MIT License"})
        status, canonical, evidence, _ = resolve_dist_license(dist)
        assert (status, canonical, evidence) == (LicenseStatus.ALLOWED, "MIT", "classifier")

    def test_proprietary_classifier_forbids_even_with_clean_metadata(self):
        dist = FakeDist(
            "x",
            headers={"Classifier": "License :: Other/Proprietary License", "License-Expression": "MIT"},
        )
        # License-Expression still wins: a classifier cannot contradict it, but a
        # package with only the proprietary classifier is refused.
        assert resolve_dist_license(dist)[0] is LicenseStatus.ALLOWED
        proprietary = FakeDist("y", headers={"Classifier": "License :: Other/Proprietary License"})
        assert resolve_dist_license(proprietary)[0] is LicenseStatus.FORBIDDEN

    def test_short_license_field_is_treated_as_an_expression(self):
        dist = FakeDist("x", headers={"License": "BSD-3-Clause"})
        status, canonical, evidence, _ = resolve_dist_license(dist)
        assert (status, canonical, evidence) == (LicenseStatus.ALLOWED, "BSD-3-Clause", "License field")

    def test_long_license_field_is_treated_as_text(self):
        body = f"Copyright (c) 2026 Someone.\n\n{TestLicenceTextSniffing.MIT_TEXT}"
        status, canonical, evidence, _ = resolve_dist_license(FakeDist("x", headers={"License": body}))
        assert (status, canonical, evidence) == (LicenseStatus.ALLOWED, "MIT", "License text")

    def test_bundled_license_file_is_the_last_resort(self):
        entry = FakeEntry(("x-1.0.dist-info", "licenses", "LICENSE"), TestLicenceTextSniffing.BSD_TEXT)
        dist = FakeDist("x", files=[entry])
        status, canonical, evidence, _ = resolve_dist_license(dist)
        assert (status, canonical, evidence) == (LicenseStatus.ALLOWED, "BSD-3-Clause", "LICENSE file")

    def test_license_files_skips_deeply_nested_and_empty_entries(self):
        entries = [
            FakeEntry(("x", "y", "z", "w", "LICENSE"), TestLicenceTextSniffing.MIT_TEXT),
            FakeEntry(("x-1.0.dist-info", "LICENSE"), "   "),
            FakeEntry(("x-1.0.dist-info", "RECORD"), TestLicenceTextSniffing.MIT_TEXT),
        ]
        assert _license_files(FakeDist("x", files=entries)) == []

    def test_nothing_identifiable_is_a_failure_not_a_pass(self):
        """The whole point of the gate: silence means 'stop', not 'ship'."""
        status, _canonical, evidence, note = resolve_dist_license(FakeDist("mystery"))
        assert status is LicenseStatus.UNKNOWN
        assert evidence == "none"
        assert "refusing to assume" in note


# --------------------------------------------------------------- dependency set


class TestDeclaredRequirements:
    def test_reads_core_extras_and_build_system(self, tmp_path: Path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[build-system]
requires = ["setuptools>=68"]

[project]
name = "demo"
dependencies = ["fastapi>=0.110", "uvicorn[standard]>=0.27"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
vision = ["scipy>=1.10"]
""",
            encoding="utf-8",
        )
        groups = declared_requirements(pyproject)
        assert set(groups) == {"core", "build-system", "dev", "vision"}
        assert groups["core"] == ["fastapi>=0.110", "uvicorn[standard]>=0.27"]
        assert groups["dev"] == ["pytest>=8.0"]

    def test_groups_restricted_to_an_install_profile(self, tmp_path: Path):
        """The scan is also runnable inside the shipped image, where only `core` and
        `vision` were installed. Naming the profile is how the answer says which
        environment it describes instead of pretending to be universal."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[project]
name = "demo"
dependencies = ["fastapi>=0.110"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
vision = ["scipy>=1.10"]
""",
            encoding="utf-8",
        )
        assert set(declared_requirements(pyproject, ["core", "vision"])) == {"core", "vision"}
        with pytest.raises(KeyError, match="no-such-group"):
            declared_requirements(pyproject, ["core", "no-such-group"])

    def test_a_profile_scan_reports_the_profile_it_ran(self, tmp_path: Path):
        report = scan_declared_dependencies(REPO_ROOT, ["core", "vision"])
        assert report.group_filter == ("core", "vision")
        assert "roots: core, vision" in report.format_text()
        assert report.to_dict()["root_groups"] == ["core", "vision"]
        # The full scan is a superset of the profile scan: filtering roots cannot widen it.
        subset = {f.name.lower() for f in report.findings}
        full = {f.name.lower() for f in scan_declared_dependencies(REPO_ROOT).findings}
        assert subset <= full

    def test_requirement_strings_are_parsed(self):
        parsed = parse_requirement("uvicorn[standard,a]>=0.27")
        assert parsed is not None and parsed.name == "uvicorn"
        assert parsed.extras == frozenset({"standard", "a"})
        assert parsed.key == "uvicorn"
        marker_only = parse_requirement('foo>=1 ; extra == "docs"')
        assert marker_only is not None and marker_only.name == "foo"
        assert parse_requirement("   ") is None


class TestDependencyClosure:
    def index(self, *dists: FakeDist) -> dict[str, list[FakeDist]]:
        index: dict[str, list[FakeDist]] = {}
        for dist in dists:
            index.setdefault(_normalise(dist.metadata["Name"]), []).append(dist)
        return index

    def test_follows_requires_transitively(self):
        index = self.index(
            FakeDist("root", headers={"Requires-Dist": ["mid>=1"]}),
            FakeDist("mid", headers={"Requires-Dist": ["leaf"]}),
            FakeDist("leaf"),
            FakeDist("unrelated"),
        )
        scope = dependency_closure([req("root")], index)
        assert {_normalise(n) for n in scope} == {"root", "mid", "leaf"}
        assert scope[_normalise("leaf")] == "mid"
        assert scope[_normalise("root")] == "pyproject.toml"

    def test_optional_extras_of_a_dependency_are_out_of_scope(self):
        """A shared machine may hold docs/test deps for unrelated reasons; they
        are not what *this* product needs, so grading them would make the gate
        depend on the host rather than on the manifest."""
        index = self.index(
            FakeDist(
                "root",
                headers={"Requires-Dist": ["needed>=1", 'sphinx ; extra == "docs"', 'hypothesis ; extra == "test"']},
            ),
            FakeDist("needed"),
            FakeDist("sphinx"),
            FakeDist("hypothesis"),
        )
        scope = dependency_closure([req("root")], index)
        assert {_normalise(n) for n in scope} == {"root", "needed"}

    def test_extras_we_actually_request_are_in_scope(self):
        index = self.index(
            FakeDist("httptools"),
            FakeDist("uvicorn", headers={"Requires-Dist": ['httptools ; extra == "standard"']}),
        )
        assert _normalise("httptools") in dependency_closure([req("uvicorn[standard]")], index)
        assert _normalise("httptools") not in dependency_closure([req("uvicorn")], index)

    def test_missing_dependency_breaks_the_cycle_not_the_scan(self):
        index = self.index(FakeDist("root", headers={"Requires-Dist": ["ghost"]}))
        scope = dependency_closure([req("root")], index)
        assert _normalise("ghost") not in scope  # reported separately as MISSING

    def test_environment_markers_decide_what_this_install_pulls_in(self):
        """`importlib-metadata ; python_version < "3.10"` is a dependency of nothing
        on 3.11. Grading it anyway would make the closure - and any lock written from
        it - depend on what a shared environment happens to hold."""
        index = self.index(
            FakeDist("alembic", headers={"Requires-Dist": [
                "mako>=1.1.3",
                'importlib-metadata>=3.6 ; python_version < "3.10"',
            ]}),
            FakeDist("mako"),
            FakeDist("importlib-metadata"),
        )
        scope = dependency_closure([req("alembic")], index)
        assert _normalise("mako") in scope
        assert _normalise("importlib-metadata") not in scope

    def test_a_marker_and_an_extra_on_one_line_survive_together(self):
        """Real metadata fuses the conditions (`sys_platform != "win32" and extra ==
        "standard"`). Evaluating such a line without binding `extra` answers false for
        every optional dependency a project legitimately asked for."""
        spec = 'httptools>=0.6.3 ; sys_platform != "win32" and extra == "standard"'
        assert marker_applies(spec, frozenset()) is False
        assert marker_applies(spec, frozenset({"standard"})) is True

    def test_the_walk_reports_what_it_did_not_follow(self):
        """An omission the report does not mention reads like coverage."""
        index = self.index(
            FakeDist("root", headers={"Requires-Dist": [
                "needed",
                'sphinx ; extra == "docs"',
                'typed-ast ; python_version < "3.8"',
            ]}),
            FakeDist("needed"),
            FakeDist("sphinx"),
            FakeDist("typed-ast"),
        )
        excluded: list[tuple[str, str, str]] = []
        scope = dependency_closure([req("root")], index, excluded=excluded)
        assert _normalise("needed") in scope
        assert {reason for reason, _, _ in excluded} == {"extra", "marker"}


class TestMarkerEvaluation:
    def test_no_marker_always_applies(self):
        assert marker_applies("numpy>=1.24") is True

    def test_a_marker_this_interpreter_fails_is_excluded(self):
        assert marker_applies('backports.zoneinfo ; python_version < "3.9"') is False
        assert marker_applies('dataclasses ; python_version < "3.7"') is False

    def test_a_marker_this_interpreter_passes_is_kept(self):
        """Written against the running interpreter, like the platform case below it. The literal
        this replaced was `python_version < "3.13"`, which is true on the 3.11 every measured
        number came from and false on 3.13 - so the licence gate's own test suite depended on the
        Python version of whoever ran it, and failed on the portability leg it was meant to keep
        honest."""
        above = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
        assert marker_applies(f'typing-extensions>=4.12 ; python_version < "{above}"') is True
        assert marker_applies(f'typing-extensions>=4.12 ; python_version >= "{above}"') is False

    def test_an_unparseable_requirement_counts_as_applicable(self):
        """Excluding a package is the direction in which a licence can hide, so
        ambiguity about a marker keeps the package in scope and the scan stricter."""
        assert marker_applies("numpy >=; &&") is True
        assert marker_applies("plain-name") is True

    def test_platform_marker_follows_the_machine_running_the_scan(self):
        """SQLAlchemy's greenlet line: false on darwin/arm64, true on the linux image.
        This is the pin `dependency_lock.TARGET_PLATFORM_PINS` exists to cover, and the
        assertion is written against this host's actual platform, not a fixed answer."""
        spec = "greenlet>=1 ; platform_machine == 'aarch64' or platform_machine == 'x86_64'"
        assert marker_applies(spec) == (platform.machine() in {"aarch64", "x86_64"})


# ------------------------------------------------------------- report behaviour


class TestScanReport:
    def test_unknown_and_missing_both_violate(self):
        report = ScanReport(
            findings=[
                LicenseFinding("a", "1", "MIT", LicenseStatus.ALLOWED, "test"),
                LicenseFinding("b", "1", None, LicenseStatus.UNKNOWN, "none"),
                LicenseFinding("c", "1", "GPL-3.0-only", LicenseStatus.FORBIDDEN, "test"),
                LicenseFinding("d", "1", None, LicenseStatus.MISSING, "core"),
                LicenseFinding("e", "1", "LGPL-2.1-or-later", LicenseStatus.CONDITIONAL, "test"),
            ]
        )
        assert {f.name for f in report.violations} == {"b", "c", "d"}
        assert [f.name for f in report.conditional] == ["e"]
        assert not report.ok

    def test_text_report_names_the_parent_of_a_violation(self):
        report = ScanReport(
            findings=[
                LicenseFinding(
                    "evil",
                    "1",
                    "AGPL-3.0-only",
                    LicenseStatus.FORBIDDEN,
                    "test",
                    note="disqualified",
                    required_by="nice",
                )
            ],
            roots=("nice",),
        )
        text = report.format_text()
        assert "VIOLATION evil" in text
        assert "required by nice" in text
        assert "RESULT: FAIL" in text

    def test_json_report_is_serialisable(self):
        report = scan_declared_dependencies(REPO_ROOT)
        payload = json.loads(json.dumps(report.to_dict()))
        assert payload["ok"] is True
        assert payload["scanned"] == len(report.findings)


# ------------------------------------------------- the gate CI actually enforces


@pytest.fixture(scope="module")
def repo_scan() -> ScanReport:
    return scan_declared_dependencies(REPO_ROOT)


class TestRepositoryConformance:
    def test_every_declared_dependency_is_permissively_licensed(self, repo_scan: ScanReport):
        problems = [f"{f.name}: {f.status.value} ({f.note})" for f in repo_scan.violations]
        assert not problems, "FC-1 violation(s):\n" + "\n".join(problems)

    @pytest.mark.parametrize(
        "package",
        ["fastapi", "uvicorn", "sqlalchemy", "alembic", "httpx", "numpy", "pillow", "pytest", "ruff", "mypy"],
    )
    def test_core_stack_is_present_and_graded(self, repo_scan: ScanReport, package: str):
        """Guards against the scan quietly scanning nothing."""
        finding = repo_scan.by_name(package)
        assert finding is not None, f"{package} was not scanned at all"
        assert finding.shippable

    def test_scan_covers_transitive_dependencies_not_just_roots(self, repo_scan: ScanReport):
        assert repo_scan.by_name("starlette") is not None  # via fastapi
        assert repo_scan.by_name("anyio") is not None  # via starlette
        assert repo_scan.by_name("mako") is not None  # via alembic

    def test_cli_licenses_exits_zero_on_this_repo(self):
        assert cli_main(["licenses", "--project-root", str(REPO_ROOT)]) == 0

    def test_cli_licenses_json_output_is_clean(self, capsys: pytest.CaptureFixture[str]):
        assert cli_main(["licenses", "--project-root", str(REPO_ROOT), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["violations"] == []

    def test_console_script_target_exists(self):
        """``[project.scripts] synthverify`` must resolve to a real callable; a
        stale target ships a broken binary in every wheel."""
        import tomllib

        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            target = tomllib.load(handle)["project"]["scripts"]["synthverify"]

        module_name, _, attribute = target.partition(":")
        module = __import__(module_name, fromlist=[attribute])
        assert callable(getattr(module, attribute, None))

def test_cli_exit_code_is_nonzero_when_a_scan_fails(monkeypatch: pytest.MonkeyPatch):
    from synthverify.compliance import licenses

    def fake_scan(_root: Path | str, groups: object = None) -> ScanReport:
        return ScanReport(
            findings=[LicenseFinding("gpl-thing", "1", "GPL-3.0-only", LicenseStatus.FORBIDDEN, "test")],
            roots=("gpl-thing",),
        )

    monkeypatch.setattr(licenses, "scan_declared_dependencies", fake_scan)
    assert cli_main(["licenses"]) == 1


def test_scan_of_a_missing_pyproject_is_a_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        scan_declared_dependencies(tmp_path)


def test_subprocess_run_of_the_gate_is_green():
    result = subprocess.run(
        [sys.executable, "-m", "synthverify.cli", "licenses", "--project-root", str(REPO_ROOT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
