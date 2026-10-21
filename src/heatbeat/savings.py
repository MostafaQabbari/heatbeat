"""Weather-normalised savings estimate (no promises, only measurement).

What this file does: compares energy use before and after a change FAIRLY,
even when the weather differs. Steps (in the spirit of VDI 3807 / IPMVP):
1. heating degree days per day: HDD = max(0, 15 degC - mean outdoor temperature)
2. baseline from the days BEFORE the change: energy = a + b * HDD (least squares)
3. for the days AFTER: predicted = what the old setup would have used in that weather
4. saving = predicted - actual, with a 95 % range from the baseline's scatter
In the simulator the true effect is known, so a test checks that this estimator
finds it (S20). That is the honest answer to "how much does it save?".
"""

import math
from dataclasses import dataclass
from statistics import linear_regression, mean, stdev

HEATING_LIMIT = 15.0  # degC, base temperature for heating degree days


@dataclass(frozen=True)
class Day:
    mean_outdoor: float
    energy_kwh: float

    @property
    def hdd(self) -> float:
        return max(0.0, HEATING_LIMIT - self.mean_outdoor)


@dataclass(frozen=True)
class SavingsEstimate:
    predicted_kwh: float
    actual_kwh: float
    saving_kwh: float
    saving_pct: float
    low_pct: float  # 95 % range
    high_pct: float
    r_squared: float
    days_before: int
    days_after: int

    def summary(self) -> str:
        return (
            f"{self.saving_pct:.1f} % saved (95 % range {self.low_pct:.1f} to {self.high_pct:.1f} %), "
            f"{self.saving_kwh:.0f} kWh over {self.days_after} days; baseline from {self.days_before} days, "
            f"R^2 = {self.r_squared:.2f}"
        )


class NotEnoughData(Exception):
    pass


def estimate(before: list[Day], after: list[Day], min_before: int = 14, min_after: int = 7) -> SavingsEstimate:
    if len(before) < min_before or len(after) < min_after:
        raise NotEnoughData(f"need {min_before} days before and {min_after} after, got {len(before)}/{len(after)}")
    x = [d.hdd for d in before]
    y = [d.energy_kwh for d in before]
    if len(set(x)) < 2:
        raise NotEnoughData("weather did not vary before the change; cannot fit a baseline")
    slope, intercept = linear_regression(x, y)
    fitted = [intercept + slope * xi for xi in x]
    residuals = [yi - fi for yi, fi in zip(y, fitted, strict=True)]
    ss_res = sum(r * r for r in residuals)
    ss_tot = sum((yi - mean(y)) ** 2 for yi in y)
    r_squared = 1 - ss_res / ss_tot if ss_tot else 0.0
    sigma = stdev(residuals) if len(residuals) > 2 else 0.0

    predicted = sum(intercept + slope * d.hdd for d in after)
    actual = sum(d.energy_kwh for d in after)
    saving = predicted - actual
    # Uncertainty of (actual - predicted) over n days: the daily scatter of the new
    # days (n * sigma^2) plus the uncertainty of the fitted line, evaluated at the
    # mean weather of the "after" period (standard regression prediction error).
    n, m = len(after), len(before)
    x_mean = mean(x)
    sxx = sum((xi - x_mean) ** 2 for xi in x)
    after_mean_hdd = mean(d.hdd for d in after)
    line_se = n * sigma * math.sqrt(1 / m + (after_mean_hdd - x_mean) ** 2 / sxx)
    total_se = math.sqrt(n * sigma**2 + line_se**2)
    pct = 100 * saving / predicted if predicted else 0.0
    margin_pct = 100 * 1.96 * total_se / predicted if predicted else 0.0
    return SavingsEstimate(
        predicted_kwh=predicted,
        actual_kwh=actual,
        saving_kwh=saving,
        saving_pct=pct,
        low_pct=pct - margin_pct,
        high_pct=pct + margin_pct,
        r_squared=r_squared,
        days_before=m,
        days_after=n,
    )
