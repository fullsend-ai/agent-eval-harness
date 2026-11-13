"""Per-case judge verdicts, shared by the HTML report and the scoring log.

One definition of "this case failed this judge", so the log printed by
``score.py judges`` cannot disagree with the red cells in ``report.py``.
Stdlib-only: report.py imports it without pulling in the scoring stack.
"""

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
ERROR = "error"


def score_band_class(val, lo, hi):
    """CSS class for a numeric score by its position in its own [lo, hi] range:
    green (top quartile) / amber (mid) / red (bottom third).

    Per-case cells are coloured by where the score sits on the judge's own
    scale — NOT against the aggregate `min_mean` threshold, which is a floor on
    the mean ACROSS cases and mis-flags valid per-case scores (e.g. a 1 on a
    0-2 judge with min_mean 1.5 would render red even though the judge passes).

    A value OFF the scale is invalid, not excellent: without the guard below a
    stray 4 from a 0-2 judge scores frac 2.0 and renders as a green pass.
    """
    if val < lo or val > hi:
        return "fail"
    span = hi - lo
    frac = (val - lo) / span if span else 0.5
    if frac >= 0.75:
        return "pass"
    if frac >= 0.375:
        return "warn"
    return "fail"


def judge_score_ranges(pairs):
    """Map judge name -> validated ``(lo, hi)`` from ``(name, score_range)`` pairs.

    Keeps only well-formed ranges (exactly two float-coercible, strictly
    increasing endpoints); anything else is dropped so callers fall back to
    treating the judge's scale as unknown instead of raising.
    """
    ranges = {}
    for name, sr in pairs:
        if not isinstance(sr, (list, tuple)) or len(sr) != 2:
            continue
        try:
            lo, hi = float(sr[0]), float(sr[1])
        except (TypeError, ValueError):
            continue
        if lo < hi:
            ranges[name] = (lo, hi)
    return ranges


def classify_judge_result(result, score_range=None):
    """Verdict of one judge on one case: PASS, FAIL, SKIP, ERROR or None.

    - ``value`` None: ERROR when the result carries an ``error`` (even an
      empty message), else SKIP
      (an ``if:`` condition was false).
    - bool: True passes, False fails.
    - numeric: fails only on a bottom-band score on the judge's declared
      ``score_range`` (see `score_band_class`); with no range the scale is
      unknown and the score is neutral, i.e. a pass.
    - anything else (or a non-dict result): None — no verdict.
    """
    if not isinstance(result, dict):
        return None
    val = result.get("value")
    if val is None:
        # Presence of the key, not truthiness: a message-less exception
        # (``raise RuntimeError()``) stores ``error == ""`` and is still an error.
        return ERROR if result.get("error") is not None else SKIP
    if val is True:
        return PASS
    if val is False:
        return FAIL
    if isinstance(val, (int, float)):
        if score_range is not None and score_band_class(val, *score_range) == "fail":
            return FAIL
        return PASS
    return None
