import pandas as pd
from app.analytics.abc_xyz import classify_abc, classify_xyz, combined_segment, recommend_policy


def test_abc_pareto_split():
    df = pd.DataFrame({
        "sku": ["A1", "A2", "B1", "C1", "C2"],
        "annual_consumption_value": [8000, 1000, 700, 200, 100],
    })
    result = classify_abc(df)
    assert result.iloc[0]["abc_class"] == "A"
    assert "C" in result["abc_class"].values


def test_abc_zero_total_value():
    df = pd.DataFrame({"sku": ["A1"], "annual_consumption_value": [0]})
    result = classify_abc(df)
    assert result.iloc[0]["abc_class"] == "C"


def test_xyz_classification_bounds():
    df = pd.DataFrame({
        "sku": ["X", "Y", "Z"],
        "mean_demand": [100, 100, 100],
        "std_demand": [10, 60, 150],  # CV = 0.1, 0.6, 1.5
    })
    result = classify_xyz(df)
    classes = dict(zip(result["sku"], result["xyz_class"]))
    assert classes["X"] == "X"
    assert classes["Y"] == "Y"
    assert classes["Z"] == "Z"


def test_xyz_zero_mean_demand_is_most_variable():
    df = pd.DataFrame({"sku": ["N"], "mean_demand": [0], "std_demand": [0]})
    result = classify_xyz(df)
    assert result.iloc[0]["xyz_class"] == "Z"


def test_combined_segment_and_policy():
    seg = combined_segment("A", "X")
    assert seg == "AX"
    assert "continuous review" in recommend_policy(seg).lower()


def test_unknown_segment_has_fallback_policy():
    assert recommend_policy("ZZ") == "No default policy configured for this segment."
