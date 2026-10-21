"""Tests for the weather-normalised savings estimate (src/heatbeat/savings.py), scenario S20.

In a simulation the TRUE saving is known, so we can check that the estimator finds it.
"""

import random

import pytest

from heatbeat.savings import Day, NotEnoughData, estimate


def days(n: int, rng: random.Random, factor: float = 1.0, noise: float = 15.0) -> list[Day]:
    """Daily energy = 50 + 12 kWh per degree day, times `factor` (0.9 = 10 % saving), plus noise."""
    out = []
    for _ in range(n):
        outdoor = rng.uniform(-8, 14)
        hdd = max(0.0, 15 - outdoor)
        out.append(Day(outdoor, factor * (50 + 12 * hdd) + rng.gauss(0, noise)))
    return out


def test_s20_recovers_a_known_ten_percent_saving():
    rng = random.Random(7)

    result = estimate(days(42, rng), days(28, rng, factor=0.9))

    assert 8.0 < result.saving_pct < 12.0
    assert result.low_pct < 10.0 < result.high_pct  # the true value lies inside the stated range
    assert result.r_squared > 0.9


def test_weather_alone_is_not_counted_as_a_saving():
    """A warmer 'after' period uses less energy, but nothing was saved."""
    rng = random.Random(3)
    before = days(42, rng)
    warm_after = [Day(d.mean_outdoor + 5, 50 + 12 * max(0.0, 15 - d.mean_outdoor - 5)) for d in days(28, rng)]

    result = estimate(before, warm_after)

    assert result.low_pct < 0 < result.high_pct  # "no saving" is inside the range
    assert abs(result.saving_pct) < 5  # instead of the ~25 % a naive before/after comparison would show


def test_s20_the_95_percent_range_is_honest():
    """Over many simulated buildings, the range contains the true saving about 95 % of the time."""
    hits = 0
    for seed in range(200):
        rng = random.Random(seed)
        result = estimate(days(42, rng), days(28, rng, factor=0.9))
        hits += result.low_pct <= 10.0 <= result.high_pct
    assert hits >= 180  # at least 90 % (95 % expected, with some sampling noise)


def test_too_few_days_is_refused():
    rng = random.Random(1)

    with pytest.raises(NotEnoughData):
        estimate(days(5, rng), days(28, rng))


def test_weather_that_never_changed_is_refused():
    with pytest.raises(NotEnoughData):
        estimate([Day(20.0, 30.0)] * 20, [Day(20.0, 30.0)] * 10)  # always above 15 degC: HDD is 0
