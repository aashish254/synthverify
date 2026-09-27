"""`AC-INFRA-6`'s second clause: the alert rules ship as a file, and that file must be honest.

Prometheus is not running here, so this gate cannot say a rule *fires*. What it can say, and does, is
the three things that make a shipped rules file worth shipping:

1. it **parses**, and every rule has the shape an operator's `promtool` would reject without;
2. every metric name in an expression is a metric **this build declares** - the drift that leaves a
   rule permanently silent is a rename on one side of the boundary;
3. the declaration itself is not fiction: every name in ``HELP_TEXTS`` appears as a string literal in
   the package, and every such literal in the package is in ``HELP_TEXTS``. A metric that is documented
   but never emitted fails check 3, and so does one emitted with no help text for the scrape to show.

The scrape side of the same contract is checked against the *live* exposition in
``tests/test_tracing.py`` and ``scripts/trace_e2e.py``, which is where an undocumented-but-emitted name
would otherwise hide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from synthverify.metrics import HELP_TEXTS

#: Where the rules live, relative to the project root.
DEFAULT_RULES_PATH = Path("docker") / "prometheus-alerts.yml"

SEVERITIES = ("critical", "warning", "info")

#: Words a PromQL expression may contain that are not metric names. Kept to the operators and
#: functions this file actually uses, so a typo in a rule cannot hide inside a long allow-list.
_PROMQL_WORDS = frozenset(
    {
        "absent",
        "absent_over_time",
        "and",
        "by",
        "clamp_min",
        "group_left",
        "group_right",
        "ignoring",
        "increase",
        "info",
        "label_replace",
        "offset",
        "on",
        "or",
        "rate",
        "sum",
        "unless",
    }
)

_LABEL_SELECTOR = re.compile(r"\{[^}]*\}")
_RANGE_SELECTOR = re.compile(r"\[[^\]]*\]")
#: `by (path)` / `without (...)` / `on (...)` name *labels*, not metrics, and a rule that breaks a
#: series down by path would otherwise be read as if "path" were a metric this build must emit.
_GROUPING_CLAUSE = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")
#: `offset 7d` is a modifier; its unit would otherwise be picked up as a metric named `d`.
_OFFSET = re.compile(r"\boffset\s+[-+]?\d+(?:ms|s|m|h|d|w|y)", re.IGNORECASE)
_IDENT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_:]*")
_METRIC_LITERAL = re.compile(r'"(synthverify_[a-z0-9_]+)"')


@dataclass(frozen=True)
class RuleIssue:
    kind: str
    detail: str


@dataclass
class RuleReport:
    """What the rules file says, and every way it disagrees with the code."""

    path: str = ""
    groups: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    metrics: set[str] = field(default_factory=set)
    declared: int = 0
    live_scrape: int = 0
    issues: list[RuleIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "groups": self.groups,
            "rules": self.rules,
            "metrics_referenced": sorted(self.metrics),
            "declared_metrics": self.declared,
            "live_scrape_series": self.live_scrape,
            "issues": [{"kind": i.kind, "detail": i.detail} for i in self.issues],
        }

    def format_text(self) -> str:
        lines = [
            f"Alert rules: {len(self.rules)} rules in {len(self.groups)} group(s) from {self.path}",
            f"  metric names referenced: {len(self.metrics)}; declared by the build: {self.declared}",
        ]
        if self.live_scrape:
            lines.append(f"  cross-checked against {self.live_scrape} name(s) from a live scrape")
        for issue in self.issues:
            lines.append(f"  {issue.kind.upper():<9} {issue.detail}")
        lines.append("  RESULT: " + ("PASS" if self.ok else f"FAIL ({len(self.issues)} problem(s))"))
        return "\n".join(lines)


def metrics_in_expr(expr: str) -> set[str]:
    """Identifiers in a PromQL expression that are not label selectors, ranges, groupings or keywords."""
    stripped = _OFFSET.sub(
        " ",
        _GROUPING_CLAUSE.sub(
            " ", _RANGE_SELECTOR.sub("", _LABEL_SELECTOR.sub("", expr or ""))
        ),
    )
    return {
        token
        for token in _IDENT.findall(stripped)
        if token not in _PROMQL_WORDS and not token.replace(".", "", 1).isdigit()
    }


def load_rules(path: Path) -> tuple[list[dict[str, Any]], list[RuleIssue]]:
    """Parse the file and check each rule's shape. Returns (rules, issues)."""
    if not path.is_file():
        return [], [RuleIssue("missing", f"{path} does not exist")]
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # the whole point of clause 2
        location = f" (line {exc.problem_mark.line + 1}, column {exc.problem_mark.column + 1})" if isinstance(exc, yaml.MarkedYAMLError) else ""
        return [], [RuleIssue("unparseable", f"{path}: {type(exc).__name__}{location}: {exc}")]
    groups = (document or {}).get("groups")
    if not isinstance(groups, list) or not groups:
        return [], [RuleIssue("shape", f"{path}: expected a non-empty top-level `groups` list")]
    rules: list[dict[str, Any]] = []
    issues: list[RuleIssue] = []
    for group in groups:
        name = group.get("name") if isinstance(group, dict) else None
        if not name:
            issues.append(RuleIssue("shape", "a group has no `name`"))
            continue
        body = group.get("rules")
        if not isinstance(body, list) or not body:
            issues.append(RuleIssue("shape", f"group {name!r} has no rules"))
            continue
        for rule in body:
            rule = dict(rule or {})
            rule["_group"] = name
            rules.append(rule)
    return rules, issues


def _check_rule(rule: dict[str, Any], report: RuleReport, seen: set[str]) -> None:
    where = f"{rule.get('_group', '?')}/{rule.get('alert') or rule.get('record') or '?'}"
    if "record" in rule:
        # A recorded series is produced by Prometheus, so the name cannot be verified against this
        # build - which is why the shipped file promises not to contain any.
        report.issues.append(RuleIssue("recording-rule", f"{where}: recording rules are not allowed here"))
        return
    alert = rule.get("alert")
    if not alert:
        report.issues.append(RuleIssue("shape", f"{where}: a rule has no `alert` name"))
        return
    if alert in seen:
        report.issues.append(RuleIssue("duplicate", f"{alert} is defined more than once"))
    seen.add(alert)
    report.rules.append(alert)
    if not str(rule.get("expr", "")).strip():
        report.issues.append(RuleIssue("shape", f"{where}: no `expr`"))
    if not str(rule.get("for", "")).strip():
        report.issues.append(RuleIssue("shape", f"{where}: no `for` - an alert without a wait pages on a blip"))
    annotations = rule.get("annotations") or {}
    for key in ("summary", "description"):
        if not str(annotations.get(key, "")).strip():
            report.issues.append(RuleIssue("shape", f"{where}: annotations.{key} is empty"))
    severity = (rule.get("labels") or {}).get("severity")
    if severity not in SEVERITIES:
        report.issues.append(
            RuleIssue("shape", f"{where}: labels.severity is {severity!r}; expected one of {SEVERITIES}")
        )


def metric_literals(package_dir: Path) -> dict[str, list[str]]:
    """Every ``synthverify_…`` string literal in the package, with the files that contain it."""
    found: dict[str, list[str]] = {}
    for source in sorted(package_dir.rglob("*.py")):
        for match in _METRIC_LITERAL.finditer(source.read_text()):
            found.setdefault(match.group(1), []).append(source.name)
    return found


def check_alert_rules(
    project_root: Path | str = ".",
    *,
    rules_path: Path | str | None = None,
    scraped_names: set[str] | None = None,
) -> RuleReport:
    """Run the gate. ``scraped_names`` optionally adds metric names seen in a live exposition.

    A name is acceptable when the build declares it in ``HELP_TEXTS`` **or** a scrape just showed it:
    the first covers metrics a run never touched, the second catches a metric that exists without ever
    having been declared.
    """
    root = Path(project_root).resolve()
    path = Path(rules_path) if rules_path else root / DEFAULT_RULES_PATH
    package = root / "synthverify"
    declared = set(HELP_TEXTS) | (scraped_names or set())
    report = RuleReport(
        path=str(path), declared=len(HELP_TEXTS), live_scrape=len(scraped_names or ())
    )

    rules, issues = load_rules(path)
    report.issues.extend(issues)
    report.groups = list(dict.fromkeys(str(rule.get("_group", "")) for rule in rules))
    seen: set[str] = set()
    for rule in rules:
        _check_rule(rule, report, seen)
        for metric in metrics_in_expr(str(rule.get("expr", ""))):
            report.metrics.add(metric)
            if metric not in declared:
                hint = " (not declared in synthverify/metrics.py:HELP_TEXTS"
                if scraped_names is None:
                    hint += ", and no live scrape was supplied to this check"
                report.issues.append(
                    RuleIssue("unknown-metric", f"{rule.get('alert')}: expr references {metric}{hint})")
                )

    if not report.rules and not issues:
        report.issues.append(RuleIssue("shape", f"{path}: parsed with no rules in it"))

    if package.is_dir():
        literals = metric_literals(package)
        for name, files in sorted(literals.items()):
            if name not in HELP_TEXTS:
                report.issues.append(
                    RuleIssue("undeclared-metric", f"{name} is emitted from {sorted(set(files))[0]} but has no HELP_TEXTS entry")
                )
        for name in sorted(set(HELP_TEXTS) - set(literals)):
            report.issues.append(
                RuleIssue("dead-declaration", f"HELP_TEXTS declares {name}, which no code in the package names")
            )
    return report
