"""The evaluator, evaluated.

`AC-DET-1b` refuses self-generated fixtures as evidence that a *detector* works. The same argument
applies one level down: a metric that has never been checked against a case you can compute on paper
is just another assertion, and here a wrong AUC silently becomes a wrong gate in `ci.yml:204`. So
every function in `synthverify/eval/metrics.py` is asserted against a hand-counted example, and the
harness output is pushed through the real FC-3 manifest validator rather than a mock of it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from synthverify.compliance.model_manifest import validate_manifest
from synthverify.eval import metrics as ev
from synthverify.eval.metrics import MIN_CELL_N, Interval, evaluate, evaluate_by_group, per_group_entries

# --------------------------------------------------------------------------------------
# input validation - the cases a metric cannot answer must be refused, not guessed at
# --------------------------------------------------------------------------------------


def test_auc_is_undefined_with_one_class_and_says_so():
    with pytest.raises(ev.InsufficientLabelsError, match="only one class"):
        ev.auc([0.1, 0.2, 0.3], [1, 1, 1])


def test_length_mismatch_is_refused():
    with pytest.raises(ev.InsufficientLabelsError, match="length mismatch"):
        ev.auc([0.1, 0.2], [1])


def test_non_finite_score_is_refused_rather_than_silently_ranked():
    with pytest.raises(ev.InsufficientLabelsError, match="non-finite"):
        ev.auc([0.1, float("nan"), 0.3], [1, 0, 1])


def test_label_outside_the_binary_domain_is_refused():
    with pytest.raises(ev.InsufficientLabelsError, match="0 \\(authentic\\)"):
        ev.auc([0.1, 0.2, 0.3], [0, 1, 2])


def test_empty_input_is_refused():
    with pytest.raises(ev.InsufficientLabelsError, match="no samples"):
        ev.ece([], [])


def test_interval_rejects_inverted_bounds():
    with pytest.raises(ValueError, match="inverted"):
        Interval(low=0.8, high=0.7)


# --------------------------------------------------------------------------------------
# AUC - Mann-Whitney with half credit for ties
# --------------------------------------------------------------------------------------


def test_auc_hand_counted_case():
    # positives 0.4, 0.6 against negatives 0.5, 0.3. Pairs: 0.4>0.3 only; 0.6>0.5 and 0.6>0.3.
    # 3 of 4 pairs win => 0.75.
    assert ev.auc([0.4, 0.6, 0.5, 0.3], [1, 1, 0, 0]) == pytest.approx(0.75)


def test_auc_of_a_constant_detector_is_exactly_chance():
    # Ties must score 0.5, not 0.0 or 1.0: this is the only convention under which "the detector
    # says nothing" reads as "no information" on the same axis as a good detector.
    assert ev.auc([0.5] * 8, [1, 0] * 4) == pytest.approx(0.5)


def test_auc_tie_between_one_positive_and_one_negative_is_half_credit():
    assert ev.auc([0.5, 0.5], [1, 0]) == pytest.approx(0.5)


def test_auc_perfect_separation_and_its_exact_inverse():
    scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    labels = [0, 0, 0, 1, 1, 1]
    assert ev.auc(scores, labels) == pytest.approx(1.0)
    assert ev.auc(scores, [1 - y for y in labels]) == pytest.approx(0.0)


def test_auc_is_invariant_to_a_strictly_monotone_rescale():
    scores = [0.11, 0.23, 0.44, 0.51, 0.87, 0.92, 0.05, 0.66]
    labels = [1, 1, 0, 1, 1, 1, 0, 0]
    assert ev.auc(scores, labels) == pytest.approx(ev.auc([1 - s for s in scores], [1 - y for y in labels]))


def test_rank_auc_equals_the_trapezoid_area_under_its_own_roc_curve():
    # Two independent routes to the same number inside this module. If the threshold sweep and the
    # midrank formula ever disagree, one of them is wrong, and this test notices without any data.
    rng = np.random.default_rng(7)
    scores = np.concatenate([rng.normal(0.35, 0.2, 120), rng.normal(0.6, 0.2, 120)])
    labels = np.array([0] * 120 + [1] * 120)
    points = ev.roc_curve(scores, labels)
    area = sum(
        (points[i + 1].fpr - points[i].fpr) * (points[i].tpr + points[i + 1].tpr) / 2.0
        for i in range(len(points) - 1)
    )
    assert area == pytest.approx(ev.auc(scores, labels), abs=1e-9)


def test_roc_curve_endpoints_and_monotonicity():
    points = ev.roc_curve([0.1, 0.4, 0.55, 0.9], [0, 0, 1, 1])
    assert points[0].fpr == 0.0 and points[0].tpr == 0.0
    assert points[-1].fpr == 1.0 and points[-1].tpr == 1.0
    fprs = [p.fpr for p in points]
    tprs = [p.tpr for p in points]
    thresholds = [p.threshold for p in points]
    assert fprs == sorted(fprs) and tprs == sorted(tprs) and thresholds == sorted(thresholds, reverse=True)


# --------------------------------------------------------------------------------------
# DeLong interval
# --------------------------------------------------------------------------------------


def test_delong_interval_brackets_the_estimate_and_stays_in_range():
    rng = np.random.default_rng(11)
    scores = np.concatenate([rng.normal(0.4, 0.2, 300), rng.normal(0.62, 0.2, 300)])
    labels = np.array([0] * 300 + [1] * 300)
    ci = ev.auc_delong_ci(scores, labels)
    assert 0.0 <= ci.low <= ev.auc(scores, labels) <= ci.high <= 1.0
    assert ci.method == "delong-logit"


def test_delong_interval_for_a_perfect_detector_does_not_claim_more_than_certainty():
    # The reason the interval is built on the logit scale: the plain normal interval on an AUC of
    # 1.0 leaks past 1, and a manifest metric that can exceed its own range fails the range check
    # in `model_manifest._validate_eval` for being plausible-looking.
    ci = ev.auc_delong_ci([0.1, 0.2, 0.3, 0.8, 0.9, 0.95], [0, 0, 0, 1, 1, 1])
    assert ci.high <= 1.0
    assert ci.low > 0.5


def test_the_delong_interval_is_the_logit_one_not_a_normal_one_clamped_to_the_range():
    # Clamping `estimate ± z·SE` to [0, 1] looks like the same fix and is measurably not it. Ten
    # synthetic at 0.80..0.99 against ten real at 0.02..0.81: AUC 0.99, SE 0.0141, so the normal
    # upper bound is 1.0142 -> clamped to *exactly* 1.0. That prints as "certainly perfect" for a
    # twenty-sample measurement, and the whole point of the CI is to refuse to say that.
    # The logit interval stays inside the range without touching it, and spends the leaked width on
    # the low side, where the uncertainty actually is.
    pos = [0.80, 0.83, 0.86, 0.88, 0.90, 0.92, 0.94, 0.95, 0.97, 0.99]
    neg = [0.02, 0.10, 0.19, 0.27, 0.35, 0.44, 0.52, 0.61, 0.72, 0.81]
    scores, labels = pos + neg, [1] * 10 + [0] * 10
    estimate, ci = ev.delong_auc(scores, labels)
    assert ci.high < 1.0, "an interval that reaches certainty on 20 samples is not an interval"
    assert estimate - ci.low > ci.high - estimate, "a symmetric band is the normal one, not the logit one"


def test_delong_s_point_estimate_is_the_mann_whitney_auc_even_when_most_scores_are_tied():
    # The one independent check on the `searchsorted` implementation: DeLong's placement mean *is*
    # the rank AUC, ties and all, and `auc()` gets there by midranks instead. Ten distinct values
    # across 22 samples is well past the point where the two derivations could be quietly different,
    # because the tie term appears in both - give a tie full credit instead of half and this estimate
    # moves to 0.45 while `auc()` stays at 0.4042.
    pos = [0.1, 0.2, 0.2, 0.3, 0.3, 0.3, 0.4, 0.5, 0.5, 0.6]
    neg = [0.0, 0.1, 0.1, 0.2, 0.4, 0.4, 0.5, 0.5, 0.6, 0.7, 0.8, 0.9]
    scores, labels = pos + neg, [1] * len(pos) + [0] * len(neg)
    assert len(set(scores)) < len(scores) // 2
    estimate, ci = ev.delong_auc(scores, labels)
    assert estimate == pytest.approx(ev.auc(scores, labels), abs=1e-12)
    assert ci.low <= estimate <= ci.high


def test_delong_interval_narrows_as_the_sample_grows():
    rng = np.random.default_rng(3)
    half = np.concatenate([rng.normal(0.4, 0.2, 200), rng.normal(0.6, 0.2, 200)])
    labels = np.array([0] * 200 + [1] * 200)
    small = ev.auc_delong_ci(half, labels)
    big = ev.auc_delong_ci(np.concatenate([half, half + 1e-6]), np.concatenate([labels, labels]))
    assert big.width < small.width


# --------------------------------------------------------------------------------------
# EER
# --------------------------------------------------------------------------------------


def test_eer_is_zero_for_a_perfectly_separating_score():
    eer, threshold = ev.eer([0.1, 0.2, 0.3, 0.7, 0.8, 0.9], [0, 0, 0, 1, 1, 1])
    assert eer == pytest.approx(0.0)
    # every sample at or above the returned threshold is synthetic, and nothing below it is
    scores = np.array([0.1, 0.2, 0.3, 0.7, 0.8, 0.9])
    assert set(scores[scores >= threshold]) == {0.7, 0.8, 0.9}


def test_eer_hand_counted_crossing():
    # negatives 0.3, 0.5 / positives 0.4, 0.6. At threshold 0.5 exactly one negative is flagged
    # (fpr = 0.5) and one positive is missed (fnr = 0.5), so the equal-error rate is 0.5.
    eer, threshold = ev.eer([0.3, 0.5, 0.4, 0.6], [0, 0, 1, 1])
    assert eer == pytest.approx(0.5)
    assert threshold == pytest.approx(0.5)


def test_eer_is_interpolated_when_the_two_curves_jump_over_each_other():
    # The case above has a threshold where the errors are *exactly* equal, which real detector scores
    # almost never do - it exercises the `d0 == 0` shortcut, not the interpolation that the docstring
    # promises. Here two positives (0.9, 0.6) and three negatives (0.8, 0.7, 0.1) give fpr 1/3 at
    # threshold 0.8 and fpr 2/3 at 0.7 while fnr sits at 0.5 for both, so the crossing is between the
    # two: halfway in fpr, hence EER 0.5 at threshold 0.75. Reading the far side of the jump instead
    # reports 0.667 at 0.7, which is a different detector's error rate.
    scores = [0.9, 0.6, 0.8, 0.7, 0.1]
    labels = [1, 1, 0, 0, 0]
    curve = {round(p.threshold, 6): (p.fpr, p.fnr) for p in ev.roc_curve(scores, labels)}
    assert curve[0.8] == (pytest.approx(1 / 3), pytest.approx(0.5))
    assert curve[0.7] == (pytest.approx(2 / 3), pytest.approx(0.5))
    eer, threshold = ev.eer(scores, labels)
    assert eer == pytest.approx(0.5)
    assert threshold == pytest.approx(0.75)


def test_eer_of_an_uninformative_score_is_near_chance():
    rng = np.random.default_rng(21)
    scores = rng.normal(0.5, 0.2, 4000)
    labels = rng.integers(0, 2, 4000)
    eer, _ = ev.eer(scores, labels)
    assert 0.42 < eer < 0.58
    assert 0.0 <= eer <= 0.5


# --------------------------------------------------------------------------------------
# average precision
# --------------------------------------------------------------------------------------


def test_average_precision_hand_counted():
    # ranked descending: 0.9(P) precision 1/1, 0.8(N), 0.7(N), 0.6(P) precision 2/4
    # AP = (1.0 + 0.5) / 2 positives = 0.75
    assert ev.ap([0.9, 0.8, 0.7, 0.6], [1, 0, 0, 1]) == pytest.approx(0.75)


def test_average_precision_of_a_perfect_ranking_is_one():
    assert ev.ap([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == pytest.approx(1.0)


def test_average_precision_ignores_the_order_the_rows_arrived_in():
    # Same pairs, two write orders. A threshold predicts *every* sample at or above it, so samples
    # sharing a score cannot be separated by rank -- and the moment they are, the number in the
    # thesis table is a property of the score table's row order rather than of the detector. Found by
    # running `synthverify eval --by-generator`: the pooled cell and the generator cell held the same
    # twelve rows and printed 0.3468 and 0.5022.
    scores = [0.9, 0.9, 0.1, 0.1, 0.1, 0.1]
    labels = [1, 0, 1, 0, 0, 0]
    shuffled = [4, 0, 5, 1, 2, 3]
    again = ([scores[i] for i in shuffled], [labels[i] for i in shuffled])
    assert ev.ap(scores, labels) == pytest.approx(ev.ap(*again))
    # Hand-counted at the only two thresholds that exist: {0.9} gives 1 of 2 correct (precision 0.5,
    # recall 0.5), {0.1} gives both positives (precision 2/6, recall 1.0).
    assert ev.ap(scores, labels) == pytest.approx(0.5 * 0.5 + (2 / 6) * 0.5)


def test_average_precision_of_a_constant_score_is_its_base_rate():
    # The control case for "this heuristic measures nothing", and the reason the tie rule above is not
    # pedantry: one threshold predicts everything positive at once, so AP is the prevalence of the
    # positive class however the rows are ordered. Crediting each positive's own rank instead reads
    # 1.0 for a detector that never varied its mind.
    tied = [0.5] * 8
    assert ev.ap(tied, [1, 1, 1, 1, 0, 0, 0, 0]) == pytest.approx(0.5)
    assert ev.ap(tied, [0, 1, 0, 1, 0, 1, 0, 1]) == pytest.approx(0.5)
    assert ev.evaluate(tied, [1, 1, 1, 1, 0, 0, 0, 0]).average_precision == pytest.approx(0.5)
    assert ev.auc(tied, [1, 1, 1, 1, 0, 0, 0, 0]) == pytest.approx(0.5)


def test_average_precision_and_auc_disagree_on_a_buried_positive():
    # Hand-counted both ways. positives {0.95, 0.9, 0.1} vs negatives {0.8, 0.7}: 4 of 6 pairs win =>
    # AUC 0.6667. AP reads precision at each positive's rank: 1/1, 2/2, 3/5 => 0.8667. Two numbers,
    # same ranking, different verdict - which is why a table that prints only one of them is not
    # reporting a detector, and why both travel in `BinaryMetrics`.
    scores = [0.95, 0.9, 0.8, 0.7, 0.1]
    labels = [1, 1, 0, 0, 1]
    assert ev.auc(scores, labels) == pytest.approx(4 / 6)
    assert ev.ap(scores, labels) == pytest.approx((1.0 + 1.0 + 0.6) / 3)
    assert ev.ap(scores, labels) > ev.auc(scores, labels)


# --------------------------------------------------------------------------------------
# confusion / operating points - the GOAL-2 numbers
# --------------------------------------------------------------------------------------


def test_confusion_hand_counted():
    c = ev.confusion_at([0.1, 0.4, 0.55, 0.9], [0, 0, 1, 1], threshold=0.5)
    assert (c.true_positive, c.false_positive, c.true_negative, c.false_negative) == (2, 0, 2, 0)
    assert c.fpr == pytest.approx(0.0)
    assert c.recall == pytest.approx(1.0)
    assert c.precision == pytest.approx(1.0)
    assert c.n == 4


def test_threshold_boundary_is_inclusive():
    # `score >= threshold means synthetic` is the rule the policy code uses; a detector that lands
    # exactly on 0.85 must be blocked, not reviewed.
    c = ev.confusion_at([0.85, 0.849999], [1, 0], threshold=0.85)
    assert c.true_positive == 1 and c.false_negative == 0


def test_precision_is_undefined_when_the_rule_raises_nothing():
    c = ev.confusion_at([0.1, 0.2], [1, 0], threshold=0.9)
    assert math.isnan(c.precision)


def test_operating_point_honours_the_max_fpr_it_was_given():
    rng = np.random.default_rng(5)
    scores = np.concatenate([rng.normal(0.45, 0.25, 1500), rng.normal(0.6, 0.25, 1500)])
    labels = np.array([0] * 1500 + [1] * 1500)
    point = ev.operating_point(scores, labels, max_fpr=0.005)
    assert point is not None
    assert point.fpr <= 0.005
    # and it is the *highest-recall* rule satisfying that, not an arbitrary one
    stricter = ev.operating_point(scores, labels, max_fpr=0.0)
    assert stricter is not None and stricter.recall <= point.recall


def test_fpr_at_fnr_answers_an_impossible_constraint_with_the_degenerate_rule_not_a_none():
    # A 0 % miss rate is always reachable by flagging everything, so the honest answer is that rule
    # with its FPR of 1.0 stated. Returning None here would let a caller read "no point found" as
    # "no risk", which is how a GOAL-2 target gets quietly missed instead of reported as missed.
    rng = np.random.default_rng(9)
    scores = rng.normal(0.5, 0.2, 400)
    labels = rng.integers(0, 2, 400)
    point = ev.fpr_at_fnr(scores, labels, max_fnr=0.0)
    assert point is not None
    assert point.recall == pytest.approx(1.0)
    assert point.fpr > 0.9
    # and on a score that separates cleanly, the same call reports the real operating point
    clean = ev.fpr_at_fnr([0.1, 0.2, 0.9], [0, 0, 1], max_fnr=0.0)
    assert clean is not None and clean.fpr == pytest.approx(0.0) and clean.recall == pytest.approx(1.0)


# --------------------------------------------------------------------------------------
# calibration - the ECE convention this thesis depends on
# --------------------------------------------------------------------------------------


def test_reliability_bins_hand_counted():
    bins = ev.reliability_curve([0.1, 0.1, 0.9, 0.9], [0, 0, 1, 1], bins=2)
    assert [b.count for b in bins] == [2, 2]
    assert bins[0].mean_score == pytest.approx(0.1) and bins[0].positive_rate == pytest.approx(0.0)
    assert bins[1].mean_score == pytest.approx(0.9) and bins[1].positive_rate == pytest.approx(1.0)
    # |0.1-0| and |0.9-1.0| both 0.1, weighted equally over 4 samples
    assert ev.ece([0.1, 0.1, 0.9, 0.9], [0, 0, 1, 1], bins=2) == pytest.approx(0.1)


def test_ece_is_zero_for_a_score_that_matches_the_observed_rate():
    # 90 of 100 at 0.9 synthetic, and 10 of 100 at 0.1 synthetic: the score *is* the probability, so
    # calibration error must be zero even though the detector is wrong about 20 % of samples. Those
    # two facts are independent, and a metric that conflates them cannot support RQ3.
    scores = [0.9] * 100 + [0.1] * 100
    labels = [1] * 90 + [0] * 10 + [1] * 10 + [0] * 90
    assert ev.ece(scores, labels, bins=2) == pytest.approx(0.0)
    assert ev.confusion_at(scores, labels, 0.5).accuracy == pytest.approx(0.9)


def test_ece_reads_a_low_score_as_a_low_probability_not_as_a_safe_decision():
    # Every sample claims 0.02; 40 % of them are in fact synthetic. The gap is 0.38 - and it is the
    # same number either calibration convention produces here, because a single-score bin is
    # symmetric. Where the two forms actually disagree is pinned by the next test, not by this one.
    scores = [0.02] * 50
    labels = [1] * 20 + [0] * 30
    assert ev.ece(scores, labels) == pytest.approx(0.38)


def test_the_calibration_convention_is_pinned_where_the_two_forms_disagree():
    # 0.9 for everything synthetic, 0.1 for everything real: a perfect ranking that is *under*-
    # confident, since the honest numbers for it would be 1.0 and 0.0.
    scores = [0.1] * 50 + [0.9] * 50
    labels = [0] * 50 + [1] * 50
    # An even bin count can drop an edge between the two bands, so each band is graded on its own.
    assert ev.ece(scores, labels, bins=2) == pytest.approx(0.1)
    # 15 requested bins put no edge between them (no multiple of 1/15 lands on the median of 100
    # samples), so all 100 collapse into one bin and the two 0.1 gaps cancel against each other.
    assert ev.ece(scores, labels, bins=15) == pytest.approx(0.0)
    assert evaluate(scores, labels, bins=15).bins_effective == 1
    # The discriminator between conventions lives in that single straddling bin: the frequency form
    # reads |0.5 - 0.5| = 0.0, the decision form would read |mean max(p,1-p) - accuracy| = 0.1.
    assert ev.ece(scores, labels, bins=1) == pytest.approx(0.0)
    assert ev.reliability_curve(scores, labels, bins=1)[0].positive_rate == pytest.approx(0.5)
    assert ev.confusion_at(scores, labels, 0.5).accuracy == pytest.approx(1.0)


def test_a_quantized_score_gets_fewer_bins_than_requested_and_says_so():
    # Several heuristics in this product emit a handful of distinct values, so "15 bins" is a
    # request, not a fact. Equal-mass edges are score quantiles, and for a 50/50 two-valued score
    # no multiple of 1/15 lands between the two values - the pool collapses into one bin, whose ECE
    # is |mean claim - mean outcome| and therefore ~0 for any balanced pool. The effective count is
    # reported so a table cannot quote that as "well calibrated".
    scores = [0.0] * 50 + [1.0] * 50
    labels = [0] * 50 + [1] * 50
    collapsed = evaluate(scores, labels, bins=15)
    assert collapsed.bins_effective == 1
    assert collapsed.ece == pytest.approx(0.0)
    assert collapsed.auc == pytest.approx(1.0)
    # an even bin count puts an edge between the bands; the score is genuinely calibrated once they
    # are graded separately, and the manifest reports which of the two it measured
    resolved = evaluate(scores, labels, bins=2)
    assert resolved.bins_effective == 2
    assert resolved.ece == pytest.approx(0.0)
    assert resolved.as_dict()["bins_effective"] == 2


def test_the_bin_count_belongs_in_the_table_because_it_moves_the_answer():
    # Direct consequence of the cancellation above: this metric is bin-count dependent, so a thesis
    # table that prints ECE without its bin count is printing half a number.
    rng = np.random.default_rng(19)
    scores = np.clip(rng.normal(0.5, 0.3, 1200), 0.01, 0.99)
    labels = rng.integers(0, 2, 1200)
    assert ev.ece(scores, labels, bins=2) <= ev.ece(scores, labels, bins=30)


def test_an_overconfident_score_with_no_signal_fails_the_manifest_range_on_calibration_alone():
    # `model_manifest._validate_eval` rejects ece outside [0, 0.2]. Here the ranking is noise (AUC
    # near 0.5) and every claim sits at ~0.999, so calibration is what trips, which is the case a
    # governance gate exists for: a detector can pass "it scored high" and still be unusable.
    rng = np.random.default_rng(13)
    n = 600
    scores = np.where(rng.integers(0, 2, n) == 1, 0.999, 0.998)
    labels = rng.integers(0, 2, n)
    report = evaluate(scores, labels)
    assert report.ece > 0.2
    assert 0.4 < report.auc < 0.6


def test_reliability_curve_collapses_to_one_bin_when_every_score_is_identical():
    bins = ev.reliability_curve([0.5] * 10, [1] * 5 + [0] * 5, bins=15)
    assert len(bins) == 1
    assert bins[0].count == 10 and bins[0].positive_rate == pytest.approx(0.5)


def test_reliability_bins_sum_to_the_sample_count():
    rng = np.random.default_rng(17)
    scores = rng.random(977)
    labels = rng.integers(0, 2, 977)
    assert sum(b.count for b in ev.reliability_curve(scores, labels, bins=15)) == 977


# --------------------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------------------


def test_bootstrap_interval_is_reproducible_from_its_seed():
    rng = np.random.default_rng(1)
    scores = np.concatenate([rng.normal(0.4, 0.2, 250), rng.normal(0.65, 0.2, 250)])
    labels = np.array([0] * 250 + [1] * 250)
    a = ev.bootstrap_ci(ev.auc, scores, labels, n_resamples=300, seed=42)
    b = ev.bootstrap_ci(ev.auc, scores, labels, n_resamples=300, seed=42)
    assert (a.low, a.high) == (b.low, b.high)
    assert "seed=42" in a.method and "n=300" in a.method


def test_bootstrap_interval_widens_for_a_smaller_sample():
    def draw(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(seed)
        scores = np.concatenate([rng.normal(0.4, 0.25, n), rng.normal(0.6, 0.25, n)])
        return scores, np.array([0] * n + [1] * n)

    small_scores, small_labels = draw(100, 2)
    big_scores, big_labels = draw(900, 2)
    wide = ev.bootstrap_ci(ev.auc, small_scores, small_labels, n_resamples=300, seed=7)
    tight = ev.bootstrap_ci(ev.auc, big_scores, big_labels, n_resamples=300, seed=7)
    assert wide.width > tight.width


def test_bootstrap_refuses_when_most_resamples_cannot_support_the_statistic():
    # three samples, one positive: most draws drop a class, and publishing the survivors' percentile
    # as a 95 % interval would be an artifact of the filter rather than a measurement
    with pytest.raises(ev.InsufficientLabelsError, match="resamples supported"):
        ev.bootstrap_ci(ev.auc, [0.1, 0.5, 0.9], [0, 0, 1], n_resamples=200, seed=1)


def test_bootstrap_requires_enough_resamples_to_be_a_percentile_interval():
    with pytest.raises(ValueError, match="too few"):
        ev.bootstrap_ci(ev.auc, [0.1, 0.2, 0.9, 0.8], [0, 0, 1, 1], n_resamples=10)


def test_bootstrap_lower_bound_is_a_percentile_not_the_median_of_the_resamples():
    # The three tests above pin that the interval is deterministic, widens as n shrinks, and refuses
    # thin data - and none of them pins *which* two numbers out of the resampling distribution come
    # out. Substituting the median for the 2.5 % quantile is a one-word edit that leaves all three
    # green and silently turns a 95 % interval into a one-sided one.
    # So use a statistic whose resampling distribution is symmetric by construction: the mean of a
    # balanced 0/1 score vector is distributed about 0.5 under resampling, and a percentile band
    # around it has two arms of the same length. On the median version the lower arm collapses to
    # zero, because the median *is* 0.5.
    scores = [0.0] * 50 + [1.0] * 50
    labels = [0] * 50 + [1] * 50
    ci = ev.bootstrap_ci(lambda s, y: float(np.mean(s)), scores, labels, n_resamples=2000, seed=11)
    assert ci.low < 0.5 < ci.high
    assert (0.5 - ci.low) == pytest.approx(ci.high - 0.5, rel=0.25)


# --------------------------------------------------------------------------------------
# the seam to governance: measured output must satisfy the real FC-3 validator
# --------------------------------------------------------------------------------------


def _manifest_data(eval_report: dict, gates: dict) -> dict:
    return {
        "schema_version": "synthverify.model-manifest/v1",
        "id": "measured-test-model",
        "detector": "image_learned",
        "media_type": "image",
        "license": "apache-2.0",
        "dataset_license": "cc0-1.0",
        "weights_source": "https://example.org/measured-test-model.pt",
        "weights_sha256": "0" * 64,
        "eval_report": eval_report,
        "gates": gates,
    }


def _balanced_groups(n_per_class: int, names: tuple[str, str] = ("a", "b")) -> list[str]:
    """Group labels for a `concat(negatives, positives)` score vector, half of each class per group.

    A contiguous block would put one class wholly inside one group, and a single-class cell has no
    AUC to measure - the breakdown would come back empty instead of coming back wrong.
    """
    half = n_per_class // 2
    block = [names[0]] * half + [names[1]] * (n_per_class - half)
    return block + block


def test_measured_report_satisfies_the_fc3_eval_validator_with_no_mocking():
    rng = np.random.default_rng(23)
    scores = np.concatenate([rng.normal(0.35, 0.18, 400), rng.normal(0.68, 0.18, 400)])
    labels = np.array([0] * 400 + [1] * 400)
    groups = np.array(["diffusion"] * 200 + ["gan"] * 200 + ["diffusion"] * 200 + ["gan"] * 200)
    pooled = evaluate(scores, labels)
    by_group = evaluate_by_group(scores, labels, groups)
    report = pooled.to_eval_report(held_out_set="unit-test synthetic split", model_id="measured-test-model")
    report["per_group"] = per_group_entries(by_group)
    issues = validate_manifest(
        _manifest_data(report, {"auc": 0.51, "eer": 0.49, "ece": 0.2}),
        source="measured",
        models_root="synthverify/models",
    )
    eval_issues = [i for i in issues if "eval_report" in str(i)]
    assert eval_issues == [], [str(i) for i in eval_issues]
    assert pooled.sufficient and all(m.sufficient for m in by_group.values())


def test_a_regression_past_the_committed_gate_is_the_thing_ci_will_catch():
    # AC-DET-3 in one assertion: the gate is a minimum for auc and a maximum for eer/ece, and the
    # validator `make model-manifests` runs must object when a measured number crosses it.
    rng = np.random.default_rng(29)
    scores = np.concatenate([rng.normal(0.4, 0.2, 300), rng.normal(0.7, 0.2, 300)])
    labels = np.array([0] * 300 + [1] * 300)
    report = evaluate(scores, labels).to_eval_report(held_out_set="unit-test synthetic split")
    report["per_group"] = per_group_entries(evaluate_by_group(scores, labels, _balanced_groups(300)))
    assert 0.5 <= report["auc"] <= 1.0 and report["eer"] <= 0.5 and report["ece"] <= 0.2
    issues = validate_manifest(
        _manifest_data(report, {"auc": 0.99, "eer": 0.001, "ece": 0.001}),
        source="regression",
        models_root="synthverify/models",
    )
    breached = {i.field for i in issues if "breaches the committed gate" in i.reason}
    assert breached == {"auc", "eer", "ece"}


def test_a_below_chance_measurement_is_called_implausible_rather_than_a_gate_result():
    # An AUC under 0.5 and an EER over 0.5 are not "a failed gate", they are a measurement that
    # cannot be what it claims to be (usually an inverted label mapping). `_validate_eval` reports
    # them on the range check and skips the gate comparison, and that distinction is worth keeping
    # honest: a thesis table should say "this number is wrong", not "this model missed its target".
    rng = np.random.default_rng(29)
    scores = np.concatenate([rng.normal(0.5, 0.3, 300), rng.normal(0.52, 0.3, 300)])
    labels = np.array([0] * 300 + [1] * 300)
    report = evaluate(scores, labels).to_eval_report(held_out_set="unit-test synthetic split")
    report["per_group"] = per_group_entries(evaluate_by_group(scores, labels, _balanced_groups(300)))
    issues = validate_manifest(
        _manifest_data(report, {"auc": 0.99, "eer": 0.001, "ece": 0.001}),
        source="inverted",
        models_root="synthverify/models",
    )
    assert any(i.field == "auc" and "outside the plausible range" in i.reason for i in issues)
    assert not any("breaches the committed gate" in i.reason for i in issues)


def test_a_gate_at_or_below_chance_is_rejected_as_proving_nothing():
    rng = np.random.default_rng(31)
    scores = np.concatenate([rng.normal(0.4, 0.2, 150), rng.normal(0.6, 0.2, 150)])
    labels = np.array([0] * 150 + [1] * 150)
    report = evaluate(scores, labels).to_eval_report(held_out_set="unit-test synthetic split")
    report["per_group"] = per_group_entries(evaluate_by_group(scores, labels, _balanced_groups(150)))
    issues = validate_manifest(
        _manifest_data(report, {"auc": 0.5, "eer": 0.49, "ece": 0.2}),
        source="weak-gate",
        models_root="synthverify/models",
    )
    assert any("at or below chance" in i.reason for i in issues)


def test_a_report_without_a_named_held_out_set_is_not_admissible():
    # the whole point of AC-DET-1b: metrics that cannot name the third-party set they came from are
    # exactly the fixture-shaped claim this amendment exists to refuse
    rng = np.random.default_rng(37)
    scores = np.concatenate([rng.normal(0.4, 0.2, 100), rng.normal(0.7, 0.2, 100)])
    labels = np.array([0] * 100 + [1] * 100)
    report = evaluate(scores, labels).to_eval_report(held_out_set="")
    report["per_group"] = per_group_entries(evaluate_by_group(scores, labels, _balanced_groups(100)))
    issues = validate_manifest(
        _manifest_data(report, {"auc": 0.51, "eer": 0.49, "ece": 0.2}),
        source="unnamed",
        models_root="synthverify/models",
    )
    assert any(i.field == "held_out_set" for i in issues)


# --------------------------------------------------------------------------------------
# group breakdowns - MIN_CELL_N is the guard against quoting n=7
# --------------------------------------------------------------------------------------


def test_thin_cells_are_flagged_rather_than_dropped():
    scores = [0.2, 0.3, 0.7, 0.8] + [0.5] * 4
    labels = [0, 1, 0, 1] + [0, 1, 0, 1]
    groups = ["tiny"] * 4 + ["also-tiny"] * 4
    by_group = evaluate_by_group(scores, labels, groups)
    entries = per_group_entries(by_group)
    assert len(entries) == 2
    assert all(e["sufficient_sample"] is False for e in entries)
    assert all(f"below {MIN_CELL_N}" in e["caveat"] for e in entries)


def test_a_group_with_only_one_class_cannot_produce_an_auc_and_is_skipped():
    scores = [0.1, 0.2, 0.3, 0.8, 0.2, 0.9, 0.3, 0.7]
    labels = [0, 0, 0, 1, 0, 1, 0, 1]
    groups = ["all-real", "all-real", "one-each", "one-each", "usable", "usable", "usable", "usable"]
    by_group = evaluate_by_group(scores, labels, groups)
    assert list(by_group) == ["usable"]
    assert by_group["usable"].n_positive == 2 and by_group["usable"].n_negative == 2


def test_group_metrics_add_up_to_the_pooled_sample_size():
    rng = np.random.default_rng(41)
    scores = rng.normal(0.5, 0.2, 600)
    labels = rng.integers(0, 2, 600)
    groups = np.array(["x", "y", "z"] * 200)
    by_group = evaluate_by_group(scores, labels, groups)
    assert sum(m.n for m in by_group.values()) == 600
    assert all(m.sufficient for m in by_group.values())


def test_to_eval_report_carries_the_counts_that_make_the_number_arguable():
    rng = np.random.default_rng(43)
    scores = np.concatenate([rng.normal(0.4, 0.2, 80), rng.normal(0.6, 0.2, 80)])
    labels = np.array([0] * 80 + [1] * 80)
    report = evaluate(scores, labels).to_eval_report(held_out_set="somewhere real")
    assert report["n"] == 160 and report["n_positive"] == 80
    assert report["auc_ci"]["method"] == "delong-logit"
    assert report["per_group"] == []
    assert report["measured_by"] == "synthverify.eval.metrics"
