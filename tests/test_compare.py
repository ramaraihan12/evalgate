from decimal import Decimal

from evalgate.compare import is_correct, is_ordered, normalize


def test_numbers_compare_across_types_and_rounding():
    assert is_correct([(Decimal("10.0"),)], [(10,)], "SELECT 1")
    assert is_correct([(10.004,)], [(10,)], "SELECT 1")
    assert not is_correct([(10.4,)], [(10,)], "SELECT 1")


def test_unordered_results_ignore_row_order():
    gold = [("a", 1), ("b", 2)]
    assert is_correct([("b", 2), ("a", 1)], gold, "SELECT x FROM t GROUP BY x")


def test_unordered_results_still_count_duplicates():
    assert not is_correct([("a",), ("a",)], [("a",)], "SELECT x FROM t")


def test_top_n_results_must_keep_order():
    sql = "SELECT n, v FROM t ORDER BY v DESC LIMIT 2"
    assert is_ordered(sql)
    assert is_correct([("a", 2), ("b", 1)], [("a", 2), ("b", 1)], sql)
    assert not is_correct([("b", 1), ("a", 2)], [("a", 2), ("b", 1)], sql)


def test_order_by_without_limit_is_not_ordered():
    assert not is_ordered("SELECT n FROM t ORDER BY n")


def test_extra_column_is_wrong():
    assert not is_correct([("a", 1, "x")], [("a", 1)], "SELECT 1")


def test_none_and_empty():
    assert is_correct([(None,)], [(None,)], "SELECT 1")
    assert is_correct([], [], "SELECT 1")
    assert not is_correct([], [(0,)], "SELECT 1")


def test_bool_is_not_a_number():
    assert normalize([(True,)], False) != normalize([(1,)], False)
