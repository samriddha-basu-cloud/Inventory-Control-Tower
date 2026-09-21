"""
ABC / XYZ / FSN classification (sections 17-18).

ABC: annual consumption value (Pareto). Configurable thresholds, default
A=top 80% cumulative value, B=next 15%, C=remaining 5% (classic 80/15/5 cut).
XYZ: coefficient of variation of demand. Default X<=0.5, Y<=1.0, Z>1.0.
FSN: movement velocity based on days since last transaction.
"""
import pandas as pd
import numpy as np


DEFAULT_ABC_THRESHOLDS = {"A": 0.80, "B": 0.95}   # cumulative value fraction cutoffs; C = remainder
DEFAULT_XYZ_THRESHOLDS = {"X": 0.5, "Y": 1.0}     # CV cutoffs; Z = remainder


def classify_abc(items_df, value_col="annual_consumption_value", thresholds=None):
    """items_df: DataFrame with a `sku` column and the value column.
    Returns the DataFrame with `abc_class` and `cum_value_pct` added."""
    thresholds = thresholds or DEFAULT_ABC_THRESHOLDS
    df = items_df.sort_values(value_col, ascending=False).copy()
    total = df[value_col].sum()
    if total <= 0:
        df["cum_value_pct"] = 0.0
        df["abc_class"] = "C"
        return df
    df["cum_value_pct"] = df[value_col].cumsum() / total

    def bucket(p):
        if p <= thresholds["A"]:
            return "A"
        if p <= thresholds["B"]:
            return "B"
        return "C"

    df["abc_class"] = df["cum_value_pct"].apply(bucket)
    return df


def classify_xyz(items_df, mean_col="mean_demand", std_col="std_demand", thresholds=None):
    thresholds = thresholds or DEFAULT_XYZ_THRESHOLDS
    df = items_df.copy()
    df["cv"] = np.where(df[mean_col] > 0, df[std_col] / df[mean_col], np.nan)

    def bucket(cv):
        if pd.isna(cv):
            return "Z"  # no reliable demand signal -> treat as most variable / riskiest
        if cv <= thresholds["X"]:
            return "X"
        if cv <= thresholds["Y"]:
            return "Y"
        return "Z"

    df["xyz_class"] = df["cv"].apply(bucket)
    return df


def classify_fsn(items_df, days_since_last_txn_col="days_since_last_txn",
                  fast_days=30, slow_days=90):
    df = items_df.copy()

    def bucket(d):
        if d is None or pd.isna(d):
            return "N"
        if d <= fast_days:
            return "F"
        if d <= slow_days:
            return "S"
        return "N"

    df["fsn_class"] = df[days_since_last_txn_col].apply(bucket)
    return df


def combined_segment(abc_class, xyz_class):
    return f"{abc_class}{xyz_class}"


SEGMENT_POLICY_RECOMMENDATIONS = {
    "AX": "High value, stable demand -> continuous review, tight safety stock, high service level.",
    "AY": "High value, moderate variability -> continuous review with buffer, monitor weekly.",
    "AZ": "High value, erratic demand -> high safety stock or make-to-order where feasible; weekly review.",
    "BX": "Moderate value, stable -> periodic review, standard service level.",
    "BY": "Moderate value, moderate variability -> periodic review, monitor monthly.",
    "BZ": "Moderate value, erratic -> consider consolidating orders, monitor closely.",
    "CX": "Low value, stable -> min-max / two-bin, low-touch management.",
    "CY": "Low value, moderate variability -> min-max, infrequent review.",
    "CZ": "Low value, erratic -> low-touch policy; consider vendor-managed or drop-ship.",
}


def recommend_policy(segment):
    return SEGMENT_POLICY_RECOMMENDATIONS.get(segment, "No default policy configured for this segment.")
