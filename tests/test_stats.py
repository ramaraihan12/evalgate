import math

import pytest

from evalgate.stats import mcnemar_one_sided, min_regressions, verdict, wilson


def test_wilson_known_values():
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)


def test_wilson_edges_stay_inside_zero_one():
    assert wilson(0, 50)[0] == 0.0
    assert wilson(50, 50)[1] == 1.0
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(0, 50)
    assert 0 < hi < 0.1


def test_mcnemar_no_discordant_cases():
    assert mcnemar_one_sided(0, 0) == 1.0


def test_mcnemar_is_exact_binomial_tail():
    assert mcnemar_one_sided(5, 0) == pytest.approx(1 / 32)
    assert mcnemar_one_sided(4, 0) == pytest.approx(1 / 16)
    assert mcnemar_one_sided(3, 1) == pytest.approx(5 / 16)
    assert mcnemar_one_sided(2, 2) == pytest.approx(11 / 16)


def test_mcnemar_is_one_sided():
    # Improvements give a p-value near 1, not a small one.
    assert mcnemar_one_sided(0, 6) == 1.0
    assert mcnemar_one_sided(1, 9) > 0.99


def test_mcnemar_large_counts_do_not_overflow():
    p = mcnemar_one_sided(300, 200)
    assert 0 < p < 1 and not math.isnan(p)


def test_min_regressions_with_no_fixes_is_five():
    assert min_regressions() == 5


def test_verdict_needs_lower_accuracy_and_small_p():
    assert verdict(30, 25, 0.03) == "FAIL"
    assert verdict(30, 25, 0.2) == "PASS"
    assert verdict(30, 31, 0.01) == "PASS"
    assert verdict(30, 30, 0.01) == "PASS"
