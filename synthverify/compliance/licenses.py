"""FC-1: the permissive-only dependency licence gate.

The rule (``docs/goal-spec.md`` FC-1): every runtime *and* build-time
dependency must be licensed under an allowlisted permissive licence. GPL, AGPL,
SSPL, BSL, "source-available" and non-commercial licences must not become
dependencies of the core product - so this module is deliberately strict about
one thing: **anything it cannot identify is a failure, not a pass.**

How a licence is identified, in order of trust:

1. ``License-Expression`` (PEP 639) - a machine-readable SPDX expression.
2. A ``License :: OSI Approved ::`` trove classifier.
3. The free-text ``License`` field, when it is a short expression.
4. The distribution's bundled ``LICENSE``/``COPYING`` file, sniffed for the
   characteristic sentences of each licence.

Compound expressions are resolved the safe way for an allowlist: ``A OR B``
passes if *any* branch passes (the installer may pick it), ``A AND B`` passes
only if *every* branch passes (both apply at once).

The dependency set is the transitive closure, over **installed** distributions,
of everything declared in ``pyproject.toml`` (core plus every extra, including
``dev`` - build-time deps are in scope), filtered by the conditions the
dependencies themselves state: an ``extra ==`` line the project did not ask for,
or a ``python_version`` marker this interpreter fails, is not something this
install pulls in. A declared requirement that is not installed is reported as a
finding rather than silently skipped, so a scan never passes on a machine that
lacks the packages it is grading.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import Any

from packaging.markers import InvalidMarker
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement as PackagingRequirement

#: Licences that are unconditionally compatible with a closed-source-capable,
#: zero-fee enterprise product (FC-1 allowlist).
ALLOWED_LICENSES: frozenset[str] = frozenset(
    {
        "MIT",
        "MIT-CMU",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "PSF-2.0",
        "MPL-2.0",
        "Unlicense",
        "0BSD",
        "Zlib",
        "BSD-4-Clause",  # original BSD, deprecated but still permissive
        # Data and model weights are rarely offered under a software licence.
        # These two are the permissive end of the content-licence world, and
        # FC-3 requires a *dataset* licence, so the allowlist has to answer.
        "CC0-1.0",
        "CDLA-Permissive-2.0",
    }
)

#: Usable only under the FC-1 caveat ("dynamically linked, never modified").
#: Python imports are dynamic linking, so these pass - but they are surfaced as
#: ``conditional`` so a human sees them on every scan.
CONDITIONAL_LICENSES: frozenset[str] = frozenset(
    {"LGPL-2.1-or-later", "LGPL-3.0-or-later", "LGPL-2.1-only", "LGPL-3.0-only"}
)

#: Named licences that FC-1 forbids outright.
FORBIDDEN_LICENSES: frozenset[str] = frozenset(
    {
        "GPL-1.0-or-later",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "AGPL-1.0-or-later",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "SSPL-1.0",
        "BUSL-1.1",
        "Elastic-2.0",
        "CC-BY-NC-4.0",
        "CC-BY-NC-SA-4.0",
        "Commons-Clause",
        "Proprietary",
    }
)

#: Short spellings that show up in metadata instead of SPDX ids.
_ALIASES: dict[str, str] = {
    "apache": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "asl 2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd": "BSD-3-Clause",
    "bsd-2": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit": "MIT",
    "isc license": "ISC",
    "psf": "PSF-2.0",
    "psf license": "PSF-2.0",
    "python software license": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.1-or-later",
    "gnu lesser general public license v2.1 or later (lgplv2.1+)": "LGPL-2.1-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.1-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu affero general public license v3 (agplv3)": "AGPL-3.0-only",
    "mit-cmu": "MIT-CMU",
    "unlicense": "Unlicense",
    "public domain": "Unlicense",
    "zlib": "Zlib",
    "public-domain": "Unlicense",
    "cc0": "CC0-1.0",
    "cc0 1.0": "CC0-1.0",
    "cc-zero": "CC0-1.0",
    "creative commons zero": "CC0-1.0",
    "cdla-permissive": "CDLA-Permissive-2.0",
    "cdla permissive 2.0": "CDLA-Permissive-2.0",
}

#: Ordered text sniffing of licence bodies. Longest/most-specific first: AGPL
#: and LGPL must be matched before the generic GPL phrase.
_TEXT_MARKERS: tuple[tuple[str, str], ...] = (
    ("gnu affero general public license", "AGPL-3.0-or-later"),
    ("gnu lesser general public license", "LGPL-2.1-or-later"),
    ("gnu library general public license", "LGPL-2.1-or-later"),
    ("gnu general public license", "GPL-3.0-or-later"),
    ("server side public license", "SSPL-1.0"),
    ("business source license", "BUSL-1.1"),
    ("elastic license", "Elastic-2.0"),
    ("noncommercial", "CC-BY-NC-4.0"),
    ("non-commercial", "CC-BY-NC-4.0"),
    ("creative commons zero", "CC0-1.0"),
    ("mozilla public license version 2.0", "MPL-2.0"),
    ("apache license version 2.0", "Apache-2.0"),
    ("permission is hereby granted, free of charge", "MIT"),
    ("the MIT/X Consortium license", "MIT"),
    ("permission to use, copy, modify, and/or distribute", "ISC"),
    ("python software foundation license", "PSF-2.0"),
    ("redistribution and use in source and binary forms", "BSD-3-Clause"),
    ("this software is provided by the copyright holder and contributors", "Zlib"),
)

_PROPRIETARY_HINTS = ("all rights reserved", "proprietary", "no license is granted")

_SHORT_FIELD_MAX = 60  # a License field longer than this is licence *text*
_LICENSE_FILE_RE = re.compile(r"^(license|licence|copying|unlicense|notice)(\.\w+)?$", re.I)


class LicenseStatus(StrEnum):
    ALLOWED = "allowed"
    CONDITIONAL = "conditional"  # LGPL-style: usable under the FC-1 caveat
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"  # cannot identify -> gate fails
    MISSING = "missing"  # declared but not installed -> cannot grade


@dataclass(frozen=True)
class LicenseFinding:
    """One distribution's resolved licence, or the reason it could not be."""

    name: str
    version: str
    license_id: str | None
    status: LicenseStatus
    evidence: str  # where the answer came from
    note: str = ""
    required_by: str = ""  # what pulled it into scope ("who brought this in?")

    @property
    def shippable(self) -> bool:
        return self.status in (LicenseStatus.ALLOWED, LicenseStatus.CONDITIONAL)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanReport:
    """Result of a dependency-closure licence scan."""

    findings: list[LicenseFinding] = field(default_factory=list)
    roots: tuple[str, ...] = ()
    project_root: str = ""
    #: ``(reason, parent, spec)`` for requirement lines the closure walk left out, so a report
    #: can state its own blind spots instead of implying that metadata it skipped was graded.
    #: Reasons are ``extra`` (not requested) and ``marker`` (not this interpreter).
    excluded: tuple[tuple[str, str, str], ...] = ()
    #: The root groups the scan was restricted to; empty means every declared group.
    group_filter: tuple[str, ...] = ()

    @property
    def violations(self) -> list[LicenseFinding]:
        return [f for f in self.findings if not f.shippable]

    @property
    def conditional(self) -> list[LicenseFinding]:
        return [f for f in self.findings if f.status is LicenseStatus.CONDITIONAL]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def excluded_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(reason for reason, _, _ in self.excluded).items()))

    def by_name(self, name: str) -> LicenseFinding | None:
        key = _normalise(name)
        return next((f for f in self.findings if _normalise(f.name) == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "roots": list(self.roots),
            "root_groups": list(self.group_filter) or None,
            "scanned": len(self.findings),
            "violations": [f.to_dict() for f in self.violations],
            "conditional": [f.name for f in self.conditional],
            "excluded": self.excluded_counts,
            "findings": [f.to_dict() for f in sorted(self.findings, key=lambda f: f.name.lower())],
        }

    def format_text(self) -> str:
        scope = f" (roots: {', '.join(self.group_filter)})" if self.group_filter else ""
        lines = [
            f"FC-1 dependency licence scan: {len(self.findings)} packages from "
            f"{len(self.roots)} declared roots{scope}"
        ]
        for finding in sorted(self.findings, key=lambda f: f.name.lower()):
            mark = "ok " if finding.shippable else "XX "
            lines.append(
                f"  {mark}{finding.name:<24} {finding.version:<14} "
                f"{(finding.license_id or finding.status.value):<22} [{finding.evidence}]"
            )
        for finding in self.violations:
            lines.append(f"  VIOLATION {finding.name}: {finding.note} (required by {finding.required_by})")
        if self.conditional:
            lines.append(
                "  note: "
                + ", ".join(f.name for f in self.conditional)
                + " are LGPL - dynamically linked only, never modified (FC-1 caveat)"
            )
        if self.excluded:
            lines.append(
                "  not followed: "
                + ", ".join(f"{reason}={count}" for reason, count in self.excluded_counts.items())
                + " requirement line(s) - an extra that was not asked for, or a marker this "
                "interpreter fails"
            )
        lines.append("  RESULT: " + ("PASS" if self.ok else f"FAIL ({len(self.violations)} violations)"))
        return "\n".join(lines)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def classify_expression(expression: str) -> tuple[LicenseStatus, str | None]:
    """Resolve a (possibly compound) licence expression to a status.

    SPDX semantics: ``A OR B`` may be satisfied by either alternative, so one
    permitted branch is enough; ``A AND B`` binds both, so every branch must be
    permitted. Splitting is parenthesis-aware, so
    ``MIT AND (GPL-3.0-or-later OR Apache-2.0)`` is not mistaken for a free choice.

    Returns ``(status, canonical_id)``; ``canonical_id`` is ``None`` when the
    expression could not be understood at all.
    """
    text = re.sub(r"\s+", " ", expression.strip()).strip().rstrip(";").strip()
    if not text or text.lower() in _NON_LICENSES:
        return LicenseStatus.UNKNOWN, None
    status, ids = _eval_expression(text)
    return status, " AND ".join(dict.fromkeys(ids)) if ids else None


_NON_LICENSES = frozenset({"other/proprietary license", "unknown", "none", "na", "n/a"})


def _eval_expression(text: str) -> tuple[LicenseStatus, list[str]]:
    # Try the whole string as one licence name *before* decomposing it: the
    # historical trove-classifier spellings contain the words "or" and "and"
    # ("GNU General Public License v2 *or* later (GPLv2+)"), and splitting
    # those up turns a known licence into nonsense.
    status, canonical = _classify_token(text)
    if status is not LicenseStatus.UNKNOWN:
        return status, [canonical] if canonical else []

    alternatives = _split_top_level(text, r"\s+or\s+")
    if len(alternatives) > 1:
        results = [_eval_expression(part) for part in alternatives]
        for branch_status, ids in results:
            # One lawful branch is enough: FC-1 constrains what we distribute,
            # and an OR grants us the choice of which licence to take it under.
            if branch_status in (LicenseStatus.ALLOWED, LicenseStatus.CONDITIONAL):
                return branch_status, ids
        if any(branch_status is LicenseStatus.FORBIDDEN for branch_status, _ in results):
            return LicenseStatus.FORBIDDEN, []
        return LicenseStatus.UNKNOWN, []

    conjuncts = _split_top_level(text, r"\s+and\s+")
    if len(conjuncts) > 1:
        results = [_eval_expression(part) for part in conjuncts]
        ids = [license_id for _, branch in results for license_id in branch]
        if any(branch_status is LicenseStatus.FORBIDDEN for branch_status, _ in results):
            return LicenseStatus.FORBIDDEN, ids
        if any(branch_status is LicenseStatus.UNKNOWN for branch_status, _ in results):
            return LicenseStatus.UNKNOWN, ids
        if any(branch_status is LicenseStatus.CONDITIONAL for branch_status, _ in results):
            return LicenseStatus.CONDITIONAL, ids
        return LicenseStatus.ALLOWED, ids

    return status, [canonical] if canonical else []


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on ``separator`` only at parenthesis depth 0."""
    pattern = re.compile(separator, re.I)
    parts: list[str] = []
    depth, index = 0, 0
    current: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            match = pattern.match(text, index)
            if match:
                parts.append("".join(current))
                current = []
                index = match.end()
                continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    cleaned = [_unwrap_parens(p) for p in parts]
    return [p for p in cleaned if p]


def _unwrap_parens(text: str) -> str:
    """Strip parentheses that wrap the whole string, and only those.

    ``"MIT AND (GPL-3.0-or-later OR Apache-2.0)"`` yields branches that arrive
    still parenthesised, while ``"Mozilla Public License 2.0 (MPL 2.0)"`` has
    parens that are part of the licence's own name. A blind ``strip("()")``
    destroys the second, so the pair must enclose the entire string.
    """
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        closes_at = 0
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closes_at = index
                    break
        if closes_at != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


#: Every id this gate knows, for exact matching after normalisation.
_KNOWN_LICENSES: frozenset[str] = ALLOWED_LICENSES | CONDITIONAL_LICENSES | FORBIDDEN_LICENSES
_KNOWN_BY_KEY: dict[str, str] = {known.lower(): known for known in _KNOWN_LICENSES}


#: Version-less and abbreviated spellings that are *not* SPDX ids, so no
#: allowlist entry matches them exactly ("BSD", "mpl-2", "gpl-3", "cc-by-nc").
#: An explicit table rather than prefix matching: "MIT AND GPL-3.0-or-later"
#: starts with "MIT", and a loose matcher would read a copyleft compound
#: expression as a permissive licence. Where a short id is genuinely ambiguous
#: it resolves to the most restrictive plausible licence ("gpl" ->
#: GPL-3.0-or-later), so a guess can only ever fail closed.
_SHORT_FORMS: dict[str, str] = {
    "bsd": "BSD-3-Clause",
    "bsd-2": "BSD-2-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd-4": "BSD-4-Clause",
    "mpl": "MPL-2.0",
    "mpl-2": "MPL-2.0",
    "gpl": "GPL-3.0-or-later",
    "gpl-2": "GPL-2.0-or-later",
    "gpl-2.0": "GPL-2.0-or-later",
    "gpl-3": "GPL-3.0-or-later",
    "gpl-3.0": "GPL-3.0-or-later",
    "lgpl": "LGPL-2.1-or-later",
    "lgpl-2": "LGPL-2.1-or-later",
    "lgpl-2.1": "LGPL-2.1-or-later",
    "lgpl-3": "LGPL-3.0-or-later",
    "agpl": "AGPL-3.0-or-later",
    "agpl-3": "AGPL-3.0-or-later",
    "sspl": "SSPL-1.0",
    "bsl": "BUSL-1.1",
    "elastic": "Elastic-2.0",
    "cc-by-nc": "CC-BY-NC-4.0",
}


def _classify_token(token: str) -> tuple[LicenseStatus, str | None]:
    raw = _unwrap_parens(re.sub(r"\s+", " ", token.strip().rstrip(";")))
    key = raw.lower().strip(".")
    canonical = _ALIASES.get(key) or _KNOWN_BY_KEY.get(key) or _SHORT_FORMS.get(key)
    if canonical is None:
        return LicenseStatus.UNKNOWN, None
    if canonical in FORBIDDEN_LICENSES:
        return LicenseStatus.FORBIDDEN, canonical
    if canonical in CONDITIONAL_LICENSES:
        return LicenseStatus.CONDITIONAL, canonical
    return LicenseStatus.ALLOWED, canonical


def sniff_licence_text(text: str) -> tuple[LicenseStatus, str | None]:
    """Identify a licence from its body text (metadata fallback #4).

    Whitespace is collapsed first: the Apache and GPL texts align their version
    number in a column of spaces, which no marker could match literally.
    """
    lowered = re.sub(r"\s+", " ", text.lower())
    for marker, license_id in _TEXT_MARKERS:
        if marker in lowered and license_id in _KNOWN_LICENSES:
            return _classify_token(license_id)
    if any(hint in lowered for hint in _PROPRIETARY_HINTS):
        return LicenseStatus.FORBIDDEN, "Proprietary"
    return LicenseStatus.UNKNOWN, None


def _license_files(dist: Distribution) -> list[str]:
    out: list[str] = []
    for entry in dist.files or []:
        parts = entry.parts
        if len(parts) > 3 or not _LICENSE_FILE_RE.match(parts[-1]):
            continue
        try:
            raw = entry.read_bytes()
        except (OSError, AttributeError):
            # AttributeError: PackagePath is a pathlib.Path in 3.11+, but the
            # metadata spec allows richer adapters; a file we cannot open is
            # simply not evidence.
            continue
        text = raw.decode("utf-8", "replace")
        if text.strip():
            out.append(text)
    return out


def resolve_dist_license(dist: Distribution) -> tuple[LicenseStatus, str | None, str, str]:
    """Return ``(status, license_id, evidence, note)`` for one distribution."""
    expression = dist.metadata.get("License-Expression")
    if expression:
        status, canonical = classify_expression(expression)
        return status, canonical, "License-Expression", _status_note(status, canonical)

    classifiers = [
        value for key, value in dist.metadata.items() if key == "Classifier" and value.startswith("License ::")
    ]
    if any(c.startswith("License :: Other/Proprietary") or "Non-FOSS" in c for c in classifiers):
        return LicenseStatus.FORBIDDEN, "Proprietary", "classifier", "classifier declares a non-open licence"
    approved = [c.rsplit("::", 1)[-1].strip() for c in classifiers if c.startswith("License :: OSI Approved ::")]
    if approved:
        status, canonical = classify_expression(" OR ".join(approved))
        if status is not LicenseStatus.UNKNOWN:
            return status, canonical, "classifier", _status_note(status, canonical)

    field_value = (dist.metadata.get("License") or "").strip()
    if field_value:
        if len(field_value) <= _SHORT_FIELD_MAX and "\n" not in field_value:
            status, canonical = classify_expression(field_value)
            if status is not LicenseStatus.UNKNOWN:
                return status, canonical, "License field", _status_note(status, canonical)
        status, canonical = sniff_licence_text(field_value)
        if status is not LicenseStatus.UNKNOWN:
            return status, canonical, "License text", _status_note(status, canonical)

    for text in _license_files(dist):
        status, canonical = sniff_licence_text(text)
        if status is not LicenseStatus.UNKNOWN:
            return status, canonical, "LICENSE file", _status_note(status, canonical)

    return (
        LicenseStatus.UNKNOWN,
        None,
        "none",
        "no machine-readable licence found: refusing to assume it is permissive",
    )


def _status_note(status: LicenseStatus, license_id: str | None) -> str:
    if status is LicenseStatus.FORBIDDEN:
        return f"{license_id} is not on the FC-1 allowlist"
    if status is LicenseStatus.CONDITIONAL:
        return f"{license_id} permitted only as a dynamically linked dependency"
    if status is LicenseStatus.UNKNOWN:
        return "licence could not be identified"
    return ""


def installed_distributions() -> dict[str, list[Distribution]]:
    """Map normalised project name -> the distribution that wins at import time.

    ``sys.path`` order decides which copy of a shadowed name is live, so only
    that copy is graded; a second copy of the same project (common in shared or
    ``--system-site-packages`` environments) is not reachable code.
    """
    index: dict[str, list[Distribution]] = {}
    for dist in distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        index.setdefault(_normalise(name), [dist])
    return index


def declared_requirements(
    pyproject_path: Path, groups: Iterable[str] | None = None
) -> dict[str, list[str]]:
    """``{group: [requirement strings]}`` from ``pyproject.toml``.

    Group ``core`` holds ``[project].dependencies``; each optional-dependency
    extra becomes its own group. FC-1 covers build-time deps too, so ``dev`` is
    not special-cased away.

    ``groups`` restricts the walk to the named ones - which is how the gate runs
    *inside the shipped image*, where only ``core`` and ``vision`` were installed.
    Grading the deployment's own closure on the deployment's own platform is the
    only way to see a dependency a marker hides from the development host, so the
    filter is a widening of coverage rather than a narrowing: it says which
    environment the answer is about.
    """
    with pyproject_path.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    all_groups: dict[str, list[str]] = {"core": list(project.get("dependencies", []))}
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        all_groups[extra] = list(reqs)
    build = data.get("build-system", {}).get("requires") or []
    if build:
        all_groups["build-system"] = list(build)
    if groups is None:
        return all_groups
    wanted = [_normalise(name) for name in groups]
    unknown = sorted(set(wanted) - set(all_groups))
    if unknown:
        raise KeyError(f"pyproject.toml declares no such dependency group(s): {', '.join(unknown)}")
    return {name: all_groups[name] for name in wanted if name in all_groups}


@dataclass(frozen=True)
class Requirement:
    """A parsed PEP 508 requirement: name plus the extras requested of it."""

    name: str
    extras: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        return _normalise(self.name)


_NAME_RE = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
_EXTRA_MARKER_RE = re.compile(r"""extra\s*==\s*['"]([A-Za-z0-9._-]+)['"]""")


def parse_requirement(spec: str) -> Requirement | None:
    """Parse ``uvicorn[standard]>=0.27`` into a ``Requirement``."""
    match = _NAME_RE.match(spec.split(";", 1)[0])
    if not match:
        return None
    extras = frozenset(e.strip() for e in (match.group(2) or "").split(",") if e.strip())
    return Requirement(name=match.group(1), extras=extras)


def gated_on_extra(spec: str) -> str | None:
    """The optional extra a requirement belongs to, if it is so gated.

    ``Requires-Dist`` lines carry ``; extra == "docs"`` for a package's own
    optional groups. Those deps were not installed *because of us* - a shared
    environment may hold them for unrelated reasons, and grading them would
    make this gate depend on whatever else is on the machine rather than on
    what SynthVerify needs.
    """
    match = _EXTRA_MARKER_RE.search(spec)
    return match.group(1) if match else None


def marker_applies(spec: str, requested: frozenset[str] = frozenset()) -> bool:
    """Whether a requirement's PEP 508 environment marker selects *this* interpreter.

    Extras are not the only conditionality in ``Requires-Dist``: alembic asks for
    ``importlib-metadata`` below Python 3.10 and valkey-py asks for ``async-timeout``
    below 3.11.3. On an interpreter that will never install them, grading them anyway
    makes the answer depend on what a shared environment happens to hold - and a lock
    regenerated from that answer pins packages pip refuses to download.

    ``requested`` are the extras asked of the *parent* package, and they are what makes
    this function safe to combine with :func:`gated_on_extra`: real metadata fuses the two
    conditions into one line (``sys_platform != "win32" and extra == "standard"``), and a
    bare ``evaluate()`` has no binding for ``extra``, so it would answer *false* for every
    optional dependency a project legitimately asked for. Evaluating once per requested
    extra answers true for the one that applies.

    A marker this module cannot parse counts as *applicable*: excluding a package from the
    scan is the direction in which a violation can hide, so ambiguity keeps it in.
    """
    try:
        marker = PackagingRequirement(spec).marker
    except InvalidRequirement:
        return True
    if marker is None:
        return True
    text = str(marker)
    if "extra" not in text:
        contexts: list[dict[str, str]] = [{}]
    else:
        contexts = [{"extra": extra} for extra in requested]
    for context in contexts:
        try:
            if marker.evaluate(context):
                return True
        except InvalidMarker:
            return True
    return False


def dependency_closure(
    roots: list[Requirement],
    index: dict[str, list[Distribution]] | None = None,
    excluded: list[tuple[str, str, str]] | None = None,
) -> dict[str, str]:
    """Map each in-scope distribution name to what pulled it in.

    Scanning installed metadata (rather than re-resolving from PyPI) keeps the
    gate offline (FC-4) and grades exactly the code that can run. Recording the
    parent makes a violation actionable: "who brought this in?" has an answer.

    ``excluded``, when a list is handed in, collects ``(reason, parent, spec)`` for every
    requirement line the walk skipped. The scan is a subtraction from the metadata a package
    publishes, so the caller can print what was subtracted instead of letting an omission look
    like coverage.
    """
    index = index if index is not None else installed_distributions()
    requested_extras: dict[str, set[str]] = {}
    for root in roots:
        requested_extras.setdefault(root.key, set()).update(root.extras)

    scope: dict[str, str] = {}
    queue: list[tuple[Requirement, str]] = [(root, "pyproject.toml") for root in roots]
    while queue:
        requirement, parent = queue.pop(0)
        key = requirement.key
        if key in scope:
            continue
        dists = index.get(key, [])
        if not dists:
            continue  # the caller reports declared-but-missing packages
        scope[key] = parent
        for dist in dists:
            dist_name = dist.metadata.get("Name") or key
            for spec in dist.metadata.get_all("Requires-Dist") or []:
                extra = gated_on_extra(spec)
                if extra is not None and extra not in requested_extras.get(key, set()):
                    if excluded is not None:
                        excluded.append(("extra", dist_name, spec))
                    continue
                if not marker_applies(spec, frozenset(requested_extras.get(key, set()))):
                    if excluded is not None:
                        excluded.append(("marker", dist_name, spec))
                    continue
                child = parse_requirement(spec)
                if child is not None and child.key not in scope:
                    queue.append((child, dist_name))
    return scope


def scan_declared_dependencies(
    project_root: Path | str = ".", groups: Iterable[str] | None = None
) -> ScanReport:
    """Grade every declared (and transitively required) dependency.

    ``groups`` limits the *roots* (see :func:`declared_requirements`); the transitive walk from
    those roots is unconditional, so the answer is "this install's closure", not "some of it".
    """
    root = Path(project_root).resolve()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise FileNotFoundError(f"No pyproject.toml in {root}")

    groups_map = declared_requirements(pyproject, groups)
    declared = [(group, req) for group, reqs in groups_map.items() for req in reqs]
    index = installed_distributions()
    roots = sorted(
        {r for r in (parse_requirement(req) for _, req in declared) if r is not None},
        key=lambda r: r.key,
    )
    excluded: list[tuple[str, str, str]] = []
    scope = dependency_closure(list(roots), index, excluded=excluded)

    findings: list[LicenseFinding] = []
    for key, parent in scope.items():
        for dist in index.get(key, []):
            status, license_id, evidence, note = resolve_dist_license(dist)
            findings.append(
                LicenseFinding(
                    name=dist.metadata.get("Name") or key,
                    version=dist.version or "unknown",
                    license_id=license_id,
                    status=status,
                    evidence=evidence,
                    note=note or f"{evidence}: {license_id}",
                    required_by=parent,
                )
            )

    for group, req in declared:
        parsed = parse_requirement(req)
        if parsed is None or parsed.key in scope:
            continue
        if not marker_applies(req):
            # Declared for an interpreter that is not this one: nothing here can install it, so
            # "missing" would be a verdict about the machine rather than about the project.
            excluded.append(("marker", f"pyproject.toml:{group}", req))
            continue
        findings.append(
            LicenseFinding(
                name=parsed.name,
                version="not installed",
                license_id=None,
                status=LicenseStatus.MISSING,
                evidence=group,
                note=f"declared in {group} but not installed: its licence cannot be graded",
                required_by=f"pyproject.toml:{group}",
            )
        )

    return ScanReport(
        findings=findings,
        roots=tuple(r.name for r in roots),
        project_root=str(root),
        excluded=tuple(excluded),
        group_filter=tuple(groups_map),
    )
