import pytest
from app.optimization import safety_stock as ss
from app.analytics.service_level import z_for_service_level, cycle_service_level_for_z


def test_z_for_service_level_95():
    z = z_for_service_level(95)
    assert 1.64 < z < 1.65


def test_z_roundtrip():
    z = z_for_service_level(97.5)
    assert abs(cycle_service_level_for_z(z) - 97.5) < 0.01


def test_z_invalid_bounds():
    with pytest.raises(ValueError):
        z_for_service_level(0)
    with pytest.raises(ValueError):
        z_for_service_level(100)
    with pytest.raises(ValueError):
        z_for_service_level(None)


def test_basic_statistical_zero_std_dev():
    result = ss.basic_statistical(0, 95)
    assert result["safety_stock"] == 0.0


def test_combined_variability_matches_manual_formula():
    result = ss.combined_variability(avg_demand_daily=50, std_dev_demand_daily=10,
                                      lead_time_days=7, std_dev_lead_time_days=2, service_level_pct=95)
    import math
    z = z_for_service_level(95)
    expected = z * math.sqrt(7 * 10**2 + 50**2 * 2**2)
    assert abs(result["safety_stock"] - round(expected, 2)) < 0.01


def test_combined_variability_zero_lead_time_and_variance():
    result = ss.combined_variability(0, 0, 0, 0, 95)
    assert result["safety_stock"] == 0.0


def test_periodic_review_includes_review_period():
    continuous = ss.continuous_review(50, 10, 7, 95, std_dev_lead_time_days=1)
    periodic = ss.periodic_review(50, 10, 7, review_period_days=5, service_level_pct=95, std_dev_lead_time_days=1)
    assert periodic["safety_stock"] > continuous["safety_stock"]


def test_seasonal_scaling():
    base = ss.combined_variability(50, 10, 7, 1, 95)["safety_stock"]
    seasonal = ss.seasonal(base, 1.5)
    assert seasonal["safety_stock"] == round(base * 1.5, 2)


def test_higher_service_level_increases_safety_stock():
    low = ss.combined_variability(50, 10, 7, 1, 90)["safety_stock"]
    high = ss.combined_variability(50, 10, 7, 1, 99.5)["safety_stock"]
    assert high > low


def test_safety_stock_never_negative():
    result = ss.demand_variability(-5, 7, 50)  # nonsensical negative demand input
    assert result["safety_stock"] >= 0.0
