"""Classification metrics for the evaluation harness - numpy only, on purpose.

scikit-learn and scipy are not dependencies of this product and adding one would put a new licence
in front of the FC-1 gate (`make licenses`) for a set of numbers that is a few hundred lines of
array code. Everything here is computed from ranks or from an explicit threshold sweep, so the
implementation can be checked against a hand-computed case: `tests/test_eval_metrics.py` does
exactly that for every metric in this module.

Two properties the thesis depends on, both easy to get wrong:

* **Ties are half-credit, not breaks.** Detector scores are discrete (several of them are integers
  or rounded), so ties between a real and a synthetic sample are common rather than exotic. AUC
  uses midranks; a tie contributes 0.5, which is the only convention under which a constant
  detector scores exactly 0.5.
* **A confidence interval is part of the metric, not an appendix.** `auc_delong_ci` is the closed
  form DeLong estimator (no resampling, deterministic), `bootstrap_ci` is the general one for
  statistics without a closed form and is always seeded, and every cell carries its `n_positive` /
  `n_negative`. `MIN_CELL_N` marks a cell too small to argue from - a per-generator breakdown of
  12 images is a number that should not survive a reviewer.

`BinaryMetrics.to_eval_report()` emits the `synthverify.model-manifest/v1` `eval_report` shape
directly, so a measured run feeds the same gate `ci.yml` already enforces instead of a hand-typed
claim.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

import numpy as np

#: Below this many samples in either class a cell is reported as insufficient rather than as a number.
MIN_CELL_N = 30

#: 95 % two-sided normal quantile, used by the closed-form intervals.
_Z_95 = 1.959963984540054


class InsufficientLabelsError(ValueError):
    """A metric that is undefined was asked for (one class absent, or an empty input)."""


@dataclass(frozen=True)
class Interval:
    """A two-sided confidence interval with the method that produced it named."""

    low: float
    high: float
    level: float = 0.95
    method: str = ""

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"interval bounds inverted: {self.low} > {self.high}")

    @property
    def width(self) -> float:
        return self.high - self.low

    def as_dict(self) -> dict[str, float | str]:
        return {
            "low": round(self.low, 6),
            "high": round(self.high, 6),
            "level": self.level,
            "method": self.method,
        }


@dataclass(frozen=True)
class Confusion:
    """Counts at one decision threshold, with the rates `GOAL-2` sets targets on."""

    threshold: float
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def n(self) -> int:
        return self.true_positive + self.false_positive + self.true_negative + self.false_negative

    @property
    def recall(self) -> float:
        """Also `tpr`, `sensitivity`, `hit rate`."""
        pos = self.true_positive + self.false_negative
        return self.true_positive / pos if pos else math.nan

    @property
    def fnr(self) -> float:
        """Also `miss rate`. `GOAL-2`'s BLOCK precision target is read at a fixed version of this."""
        return 1.0 - self.recall

    @property
    def specificity(self) -> float:
        neg = self.true_negative + self.false_positive
        return self.true_negative / neg if neg else math.nan

    @property
    def fpr(self) -> float:
        """`GOAL-2`: BLOCK false-positive rate must stay at or below 0.5 %."""
        return 1.0 - self.specificity

    @property
    def precision(self) -> float:
        """Fraction of raised flags that were correct. Undefined when the rule flags nothing."""
        raised = self.true_positive + self.false_positive
        return self.true_positive / raised if raised else math.nan

    @property
    def accuracy(self) -> float:
        return (self.true_positive + self.true_negative) / self.n if self.n else math.nan

    def as_dict(self) -> dict[str, float | int]:
        return {
            "threshold": round(self.threshold, 6),
            "tp": self.true_positive,
            "fp": self.false_positive,
            "tn": self.true_negative,
            "fn": self.false_negative,
            "precision": _r(self.precision),
            "recall": _r(self.recall),
            "fpr": _r(self.fpr),
            "fnr": _r(self.fnr),
            "accuracy": _r(self.accuracy),
        }


@dataclass(frozen=True)
class RocPoint:
    """One operating point of the score ordering, labelled with its threshold."""

    threshold: float
    fpr: float
    tpr: float

    @property
    def fnr(self) -> float:
        return 1.0 - self.tpr


@dataclass(frozen=True)
class Bin:
    """One equal-mass reliability bin: what the detector claimed, and what the labels said."""

    low: float
    high: float
    count: int
    mean_score: float
    positive_rate: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "low": round(self.low, 6),
            "high": round(self.high, 6),
            "count": self.count,
            "mean_score": _r(self.mean_score),
            "positive_rate": _r(self.positive_rate),
        }


@dataclass(frozen=True)
class BinaryMetrics:
    """Everything measured about one (scores, labels) pair at one decision threshold."""

    n: int
    n_positive: int
    n_negative: int
    threshold: float
    auc: float
    auc_ci: Interval
    average_precision: float
    eer: float
    eer_threshold: float
    ece: float
    bins_effective: int
    confusion: Confusion
    roc: tuple[RocPoint, ...] = field(default=(), repr=False)
    reliability: tuple[Bin, ...] = field(default=(), repr=False)

    @property
    def sufficient(self) -> bool:
        return self.n_positive >= MIN_CELL_N and self.n_negative >= MIN_CELL_N

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "threshold": round(self.threshold, 6),
            "auc": _r(self.auc),
            "auc_ci": self.auc_ci.as_dict(),
            "average_precision": _r(self.average_precision),
            "eer": _r(self.eer),
            "eer_threshold": _r(self.eer_threshold),
            "ece": _r(self.ece),
            "bins_effective": self.bins_effective,
            "confusion": self.confusion.as_dict(),
            "sufficient_sample": self.sufficient,
        }

    def to_eval_report(self, *, held_out_set: str, model_id: str = "") -> dict[str, object]:
        """Emit the `synthverify.model-manifest/v1` `eval_report` object from measured numbers.

        `per_group` is deliberately *not* invented here: FC-3 wants error rates per demographic
        group, and a group that was never measured cannot be derived from the pooled run. Pass it
        through `groups=` at the call site once the breakdown exists.
        """
        report: dict[str, object] = {
            "auc": _r(self.auc),
            "auc_ci": self.auc_ci.as_dict(),
            "eer": _r(self.eer),
            "ece": _r(self.ece),
            "ece_bins_effective": self.bins_effective,
            "average_precision": _r(self.average_precision),
            "held_out_set": held_out_set,
            "per_group": [],
            "n": self.n,
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "measured_by": "synthverify.eval.metrics",
        }
        if model_id:
            report["model"] = model_id
        return report


def _r(value: float, digits: int = 6) -> float:
    """Round for reporting without letting `round()` turn a NaN into something printable."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return math.nan
    return round(float(value), digits)


def _as_pairs(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Validate and coerce inputs, failing loudly on the cases a metric cannot answer."""
    s = np.asarray(scores, dtype=np.float64).ravel()
    y = np.asarray(labels, dtype=np.int64).ravel()
    if s.size != y.size:
        raise InsufficientLabelsError(f"score/label length mismatch: {s.size} vs {y.size}")
    if s.size == 0:
        raise InsufficientLabelsError("no samples")
    if not np.isfinite(s).all():
        bad = int((~np.isfinite(s)).sum())
        raise InsufficientLabelsError(f"{bad} non-finite score(s); drop or repair them before scoring")
    if not np.isin(y, (0, 1)).all():
        raise InsufficientLabelsError("labels must be 0 (authentic) or 1 (synthetic)")
    n_pos = int(y.sum())
    if n_pos == 0 or n_pos == y.size:
        raise InsufficientLabelsError("AUC is undefined with only one class present")
    return s, y


def _midranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks with tied values replaced by their mean rank."""
    order = np.argsort(values, kind="stable")
    v = values[order]
    new_group = np.concatenate(([True], v[1:] != v[:-1]))
    group = np.cumsum(new_group) - 1
    counts = np.bincount(group)
    sums = np.bincount(group, weights=np.arange(1, v.size + 1, dtype=np.float64))
    ranks_sorted = np.repeat(sums / counts, counts)
    ranks = np.empty(v.size, dtype=np.float64)
    ranks[order] = ranks_sorted
    return ranks


def auc(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray) -> float:
    """Mann-Whitney U / ROC area. Ties count as half, so a constant detector returns exactly 0.5."""
    s, y = _as_pairs(scores, labels)
    n_pos = int(y.sum())
    n_neg = y.size - n_pos
    ranks = _midranks(s)
    rank_sum_pos = float(ranks[y == 1].sum())
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _delong_placement(pos: np.ndarray, neg: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """DeLong's structural components, computed with `searchsorted` instead of an n_pos x n_neg matrix.

    The outer-product form needs ~160 MB for a 4,500 x 4,500 cell - survivable once, ruinous inside
    a bootstrap loop - and this form is exact: the placement value of one score is the fraction of
    the opposite class it beats, with ties at half.
    """
    pos = np.sort(pos)
    neg = np.sort(neg)
    below = np.searchsorted(neg, pos, side="left")
    equal = np.searchsorted(neg, pos, side="right") - below
    v10 = (below + 0.5 * equal) / neg.size
    above = pos.size - np.searchsorted(pos, neg, side="right")
    equal_neg = np.searchsorted(pos, neg, side="right") - np.searchsorted(pos, neg, side="left")
    v01 = (above + 0.5 * equal_neg) / pos.size
    return v10, v01, float(v10.mean())


def delong_auc(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray, level: float = 0.95) -> tuple[float, Interval]:
    """The AUC by DeLong's placement estimator, with its closed-form interval on the logit scale.

    Returns `(estimate, interval)`. The estimate is exposed as well as the interval because of a
    theorem worth having a test on: DeLong's placement mean **is** the Mann-Whitney AUC, ties and all,
    so it must agree with `auc()` to machine precision. It is the only cross-check on this
    O(n log n) `searchsorted` implementation that does not depend on the implementation itself - the
    quadratic outer-product form is trivially correct and unusable inside a bootstrap, so if the fast
    form is quietly wrong about a tied block, `auc()` says so and nothing else would.

    The logit transform is DeLong's own recommendation: the plain normal interval leaks past 1.0 for
    an AUC near 0.99, which is precisely where a good detector lives, and an interval that includes
    impossible values invalidates the cell it is attached to. Clamping a normal interval to `[0, 1]`
    looks like the same fix and is not: it pins the upper bound to exactly 1.0 in precisely that
    high-AUC case, which reads as "certainly perfect" rather than "measured at 0.996 with a real
    sample behind it".
    """
    s, y = _as_pairs(scores, labels)
    pos, neg = s[y == 1], s[y == 0]
    if pos.size < 2 or neg.size < 2:
        raise InsufficientLabelsError(
            "DeLong's interval needs at least 2 samples of each class - with one, the variance of "
            "the placement values is undefined and the estimator would return NaN silently"
        )
    v10, v01, estimate = _delong_placement(pos, neg)
    var = float(np.var(v10, ddof=1) / pos.size + np.var(v01, ddof=1) / neg.size)
    se = math.sqrt(var)
    z = _z_for(level)
    clip = 1e-12
    a = min(max(estimate, clip), 1 - clip)
    logit = math.log(a / (1 - a))
    se_logit = se / (a * (1 - a)) if a * (1 - a) > 0 else 0.0
    low = 1.0 / (1.0 + math.exp(-(logit - z * se_logit)))
    high = 1.0 / (1.0 + math.exp(-(logit + z * se_logit)))
    return estimate, Interval(low=low, high=high, level=level, method="delong-logit")


def auc_delong_ci(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray, level: float = 0.95) -> Interval:
    """The DeLong interval alone, for callers that already have the AUC from `auc()`."""
    return delong_auc(scores, labels, level=level)[1]


def _z_for(level: float) -> float:
    if abs(level - 0.95) < 1e-9:
        return _Z_95
    return NormalDist().inv_cdf(0.5 + level / 2.0)


def roc_curve(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray) -> list[RocPoint]:
    """Every distinct operating point, thresholds descending, including the two trivial ends."""
    s, y = _as_pairs(scores, labels)
    order = np.argsort(-s, kind="stable")
    s_sorted, y_sorted = s[order], y[order]
    n_pos, n_neg = int(y.sum()), int((y == 0).sum())
    points = [RocPoint(threshold=float(s_sorted[0]) + 1.0, fpr=0.0, tpr=0.0)]
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(1 - y_sorted)
    for i in range(s_sorted.size):
        if i + 1 < s_sorted.size and s_sorted[i + 1] == s_sorted[i]:
            continue  # one point per distinct score, taken at its highest count
        points.append(
            RocPoint(
                threshold=float(s_sorted[i]),
                fpr=float(fp[i]) / n_neg,
                tpr=float(tp[i]) / n_pos,
            )
        )
    return points


def ap(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray) -> float:
    """Average precision: precision at each distinct score threshold, weighted by its recall step.

    Reported beside AUC because the two disagree exactly when the positives are not at the top but
    are still ranked above most negatives - the shape of a detector that is confident about the
    wrong things.

    Ties are credited as a group, the same rule `roc_curve` uses, and this is not a stylistic choice:
    a threshold predicts *every* sample at or above it positive, so samples sharing a score cannot be
    separated by rank. Summing precision at each positive's own position instead does exactly that --
    and makes the result depend on the order the rows arrived in. A detector that emits a constant
    score, which is the control case for "this heuristic measures nothing", then scores AP 1.0 when its
    positives happen to be written first and 0.5 when they are not. Nothing in a benchmark result may
    be a property of file order.
    """
    s, y = _as_pairs(scores, labels)
    n_pos = int(y.sum())
    order = np.argsort(-s, kind="stable")
    s_sorted, y_sorted = s[order], y[order]
    tp = np.cumsum(y_sorted)
    # Last index of each run of equal scores: the only place a threshold can actually sit.
    ends = np.nonzero(np.r_[s_sorted[1:] != s_sorted[:-1], True])[0]
    precision_at = tp[ends] / (ends + 1)
    recall_at = tp[ends] / n_pos
    delta_recall = np.diff(np.r_[0.0, recall_at])
    return float((precision_at * delta_recall).sum())


def eer(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray) -> tuple[float, float]:
    """Equal-error rate and its threshold, interpolated across the crossing point.

    Returns `(eer, threshold)`. With discrete scores the two curves usually jump over each other
    rather than touch, so the crossing is interpolated between the adjacent operating points;
    reporting the jump instead would overstate the error rate at every threshold a policy could
    actually be set to.
    """
    points = [p for p in roc_curve(scores, labels) if 0.0 <= p.fpr <= 1.0]
    diffs = [p.fpr - p.fnr for p in points]
    for i in range(len(points) - 1):
        d0, d1 = diffs[i], diffs[i + 1]
        if d0 == 0.0:
            return points[i].fpr, points[i].threshold
        if d0 > 0.0 > d1 or d0 < 0.0 < d1:
            weight = d0 / (d0 - d1)
            return (
                points[i].fpr + weight * (points[i + 1].fpr - points[i].fpr),
                points[i].threshold + weight * (points[i + 1].threshold - points[i].threshold),
            )
    best = min(points, key=lambda p: abs(p.fpr - p.fnr))
    return best.fpr, best.threshold


def confusion_at(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray, threshold: float) -> Confusion:
    """Counts for the rule `score >= threshold means synthetic`."""
    s, y = _as_pairs(scores, labels)
    flagged = s >= threshold
    return Confusion(
        threshold=float(threshold),
        true_positive=int((flagged & (y == 1)).sum()),
        false_positive=int((flagged & (y == 0)).sum()),
        true_negative=int((~flagged & (y == 0)).sum()),
        false_negative=int((~flagged & (y == 1)).sum()),
    )


def reliability_curve(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray, bins: int = 15) -> list[Bin]:
    """Equal-mass calibration bins: how many samples, what they claimed, how often they were synthetic.

    Equal-mass (quantile) rather than equal-width because a detector that puts 95 % of its output in
    one narrow band would fill one bar and leave fourteen empty, which reads as "well calibrated" in
    the wrong direction.

    The comparison is `mean_score` against the **observed synthetic rate**, not against accuracy at a
    decision threshold - calibration in the large, the binary-forecast definition (Murphy 1973), and
    the one that pairs with a proper scoring rule. The other published convention is
    `|mean max(p, 1-p) - accuracy|`, and it cannot carry RQ3: it grades a detector at whatever
    threshold the caller picked, so the same 9,000 scores yield a different calibration number under
    a `block 0.85` policy than under `block 0.70`, and a number that moves with a policy choice is
    not evidence for changing that policy.

    The cost of the choice is named rather than hidden: a bin whose scores straddle the decision line
    can average out to calibrated while its individual claims are overconfident, because over- and
    under-confidence cancel inside one bin. Fifteen equal-mass bins make that rare, and
    `tests/test_eval_metrics.py` pins the convention, so switching it later is a decision with a
    failing test in front of it instead of a silent redefinition of a headline metric.
    """
    s, y = _as_pairs(scores, labels)
    if bins < 1:
        raise ValueError(f"bins must be >= 1, got {bins}")
    edges = np.unique(np.quantile(s, np.linspace(0.0, 1.0, bins + 1)))
    if edges.size < 2:
        return [Bin(float(s.min()), float(s.max()), int(s.size), float(s.mean()), float(y.mean()))]
    edges[-1] = max(edges[-1], float(np.nextafter(edges[-1], np.inf)))
    idx = np.clip(np.searchsorted(edges, s, side="right") - 1, 0, edges.size - 2)
    out: list[Bin] = []
    for b in range(edges.size - 1):
        mask = idx == b
        count = int(mask.sum())
        if count == 0:
            continue
        out.append(
            Bin(
                low=float(edges[b]),
                high=float(edges[b + 1]),
                count=count,
                mean_score=float(s[mask].mean()),
                positive_rate=float(y[mask].mean()),
            )
        )
    return out


def ece_from_curve(curve: Sequence[Bin]) -> float:
    """The count-weighted mean gap for an already-computed reliability curve."""
    total = sum(b.count for b in curve)
    if not curve or not total:
        return math.nan
    return sum(b.count * abs(b.mean_score - b.positive_rate) for b in curve) / total


def ece(scores: Sequence[float], labels: Sequence[int], bins: int = 15) -> float:
    """Expected calibration error: count-weighted mean gap between claimed score and observed rate.

    `GOAL-2` says calibration is measured, not asserted, and `model_manifest` fails a manifest whose
    `ece` is outside `[0, 0.2]`. The input is read as "probability this sample is synthetic", so an
    *unrecalibrated* raw detector score is expected to score badly here - that is RQ3's baseline, not
    a defect in this function.

    Read the result next to its **effective** bin count, not the `bins=` that was asked for:
    equal-mass edges come from the score quantiles, so a score taking few distinct values (several
    heuristics emit a handful of integers) can collapse 15 requested bins into 1 - and a single bin
    reports |mean claim - mean outcome|, which is near zero for almost any balanced pool.
    `BinaryMetrics.bins_effective` carries it, because an ECE printed without it is a number whose
    meaning depends on how quantized the detector happened to be.
    """
    s, y = _as_pairs(scores, labels)
    return ece_from_curve(reliability_curve(s, y, bins=bins))


def fpr_at_fnr(scores: Sequence[float], labels: Sequence[int], max_fnr: float) -> Confusion | None:
    """Best operating point that misses no more than `max_fnr` of the synthetic class.

    Returns `None` when the constraint is unreachable, which `GOAL-2` needs stated as a result
    rather than answered with the closest point that quietly breaks it.
    """
    best: Confusion | None = None
    for point in roc_curve(scores, labels):
        if point.fnr > max_fnr:
            continue
        c = confusion_at(scores, labels, point.threshold)
        if c.fnr > max_fnr:
            continue
        if best is None or c.fpr < best.fpr:
            best = c
    return best


def operating_point(
    scores: Sequence[float], labels: Sequence[int], max_fpr: float
) -> Confusion | None:
    """Highest-recall rule whose false-positive rate stays at or under `max_fpr`.

    This is the `GOAL-2` question - "at the point where BLOCK FPR <= 0.5 %, is precision >= 90 %?" -
    read off one call instead of argued from a table.
    """
    best: Confusion | None = None
    for point in roc_curve(scores, labels):
        if point.fpr > max_fpr:
            continue
        c = confusion_at(scores, labels, point.threshold)
        if c.fpr > max_fpr:
            continue
        if best is None or c.recall > best.recall:
            best = c
    return best


def bootstrap_ci(
    metric: Callable[[np.ndarray, np.ndarray], float],
    scores: Sequence[float],
    labels: Sequence[int],
    *,
    n_resamples: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> Interval:
    """Percentile bootstrap CI for any statistic, seeded so a re-run prints the same interval.

    Rows are resampled as (score, label) pairs, never as scores alone - resampling the classes
    independently would invent a base rate. Degenerate draws (one class missing) are dropped and
    counted; if too many were dropped the interval is refused rather than published on a minority
    of the resamples.
    """
    s, y = _as_pairs(scores, labels)
    if n_resamples < 100:
        raise ValueError(f"n_resamples={n_resamples} is too few for a percentile interval")
    rng = np.random.default_rng(seed)
    alpha = (1.0 - level) / 2.0
    values: list[float] = []
    degenerate = 0
    for _ in range(n_resamples):
        idx = rng.integers(0, s.size, size=s.size)
        bs, by = s[idx], y[idx]
        if by.sum() == 0 or by.sum() == by.size:
            degenerate += 1
            continue
        value = metric(bs, by)
        if value is not None and math.isfinite(float(value)):
            values.append(float(value))
    if len(values) < 0.8 * n_resamples:
        raise InsufficientLabelsError(
            f"only {len(values)}/{n_resamples} resamples supported this statistic "
            f"({degenerate} degenerate); the interval would be an artifact of the survivors"
        )
    arr = np.asarray(values)
    return Interval(
        low=float(np.quantile(arr, alpha)),
        high=float(np.quantile(arr, 1.0 - alpha)),
        level=level,
        method=f"bootstrap-percentile(n={len(values)},seed={seed})",
    )


def evaluate(
    scores: Sequence[float],
    labels: Sequence[int],
    *,
    threshold: float = 0.5,
    bins: int = 15,
    level: float = 0.95,
) -> BinaryMetrics:
    """The whole battery for one labelled score vector, with the closed-form AUC interval."""
    s, y = _as_pairs(scores, labels)
    estimate = auc(s, y)
    eer_value, eer_threshold = eer(s, y)
    curve = reliability_curve(s, y, bins=bins)
    return BinaryMetrics(
        n=int(s.size),
        n_positive=int(y.sum()),
        n_negative=int((y == 0).sum()),
        threshold=float(threshold),
        auc=estimate,
        auc_ci=auc_delong_ci(s, y, level=level),
        average_precision=ap(s, y),
        eer=eer_value,
        eer_threshold=eer_threshold,
        ece=ece_from_curve(curve),
        bins_effective=len(curve),
        confusion=confusion_at(s, y, threshold),
        roc=tuple(roc_curve(s, y)),
        reliability=tuple(curve),
    )


def evaluate_by_group(
    scores: Sequence[float],
    labels: Sequence[int],
    groups: Sequence[object],
    **kwargs: Any,
) -> dict[str, BinaryMetrics]:
    """Same battery per group - generator, demographic band, source corpus - with counts preserved.

    Cells that are merely thin are returned with `sufficient == False` so the caller can still print
    them: a breakdown that hides small cells is how a per-generator result ends up quoted from n=7.
    Cells that cannot carry the metric at all (fewer than 2 of either class, where AUC and its
    variance are both undefined) are omitted rather than answered with a number that has no meaning.
    """
    s, y = _as_pairs(scores, labels)
    g = np.asarray(groups, dtype=object).ravel()
    if g.size != s.size:
        raise InsufficientLabelsError(f"group/score length mismatch: {g.size} vs {s.size}")
    out: dict[str, BinaryMetrics] = {}
    for name in sorted({str(x) for x in g}):
        mask = g == name
        if int((y[mask] == 1).sum()) < 2 or int((y[mask] == 0).sum()) < 2:
            continue
        out[name] = evaluate(s[mask], y[mask], **kwargs)
    return out


def per_group_entries(metrics_by_group: Mapping[str, BinaryMetrics]) -> list[dict[str, object]]:
    """Shape a group breakdown for the manifest's `per_group[]`, which FC-3 requires non-empty."""
    entries: list[dict[str, object]] = []
    for name, m in metrics_by_group.items():
        entry: dict[str, object] = {
            "group": name,
            "auc": _r(m.auc),
            "eer": _r(m.eer),
            "fpr": _r(m.confusion.fpr),
            "fnr": _r(m.confusion.fnr),
            "n": m.n,
            "sufficient_sample": m.sufficient,
        }
        if not m.sufficient:
            entry["caveat"] = f"n_positive={m.n_positive}, n_negative={m.n_negative} (below {MIN_CELL_N})"
        entries.append(entry)
    return entries


__all__ = [
    "MIN_CELL_N",
    "BinaryMetrics",
    "Bin",
    "Confusion",
    "InsufficientLabelsError",
    "Interval",
    "RocPoint",
    "ap",
    "auc",
    "auc_delong_ci",
    "bootstrap_ci",
    "confusion_at",
    "ece",
    "ece_from_curve",
    "evaluate",
    "evaluate_by_group",
    "eer",
    "fpr_at_fnr",
    "operating_point",
    "per_group_entries",
    "reliability_curve",
    "roc_curve",
]
