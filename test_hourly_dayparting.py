from unittest.mock import patch

import budget_dayparting
import hourly_dayparting


def test_derives_best_hours_from_conversion_and_roas():
    rows = [
        {"hour_eastern": 18, "clicks": 80, "orders": 8, "spend": 40, "sales": 160},
        {"hour_eastern": 21, "clicks": 70, "orders": 10, "spend": 45, "sales": 225},
        {"hour_eastern": 10, "clicks": 90, "orders": 2, "spend": 70, "sales": 55},
        {"hour_eastern": 14, "clicks": 60, "orders": 5, "spend": 35, "sales": 120},
    ]
    with patch.object(hourly_dayparting, "PRIME_HOUR_COUNT", 2):
        schedule = hourly_dayparting.derive_schedule(rows, 10, 20)
    assert schedule["source"] == "amazon_hourly_conversion"
    assert schedule["prime_hours"] == [18, 21]


def test_insufficient_hourly_data_uses_safe_fallback():
    schedule = hourly_dayparting.derive_schedule([
        {"hour_eastern": 21, "clicks": 4, "orders": 1, "spend": 2, "sales": 20}
    ], 10, 20)
    assert schedule["source"] == "fixed_fallback"
    assert schedule["prime_hours"] == list(range(10, 21))


def test_current_mode_uses_data_selected_hours():
    schedule = {"prime_hours": [18, 19, 20, 21, 22]}
    with patch.object(budget_dayparting, "load_schedule", return_value=schedule):
        assert budget_dayparting.get_budget_protection_mode(21) == "PRIME"
        assert budget_dayparting.get_budget_protection_mode(23) == "TAPER"