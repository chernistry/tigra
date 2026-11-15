import json
import math
import random
from datetime import date, datetime, timedelta

import pytest

from tiegraph import temporal

MONDAY = date(2024, 1, 1)


def test_bh_and_by_known_values():
    p = [0.01, 0.04, 0.03, 0.005]
    assert temporal.bh(p) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    harmonic = 1 + 1 / 2 + 1 / 3 + 1 / 4
    assert temporal.by(p) == pytest.approx([0.02 * harmonic, 0.04 * harmonic, 0.04 * harmonic, 0.02 * harmonic])
    assert temporal.bh([]) == [] and temporal.by([]) == []
    # step-up: a larger p-value lowers the q of smaller ones (raw p*m/rank would be 0.12, 0.0675, 0.05)
    assert temporal.bh([0.04, 0.045, 0.05]) == pytest.approx([0.05, 0.05, 0.05])


def test_binomial_sum_and_hypergeometric_tails():
    assert temporal.binom_sum_sf([(10, 0.5)], 8) == pytest.approx(56 / 1024)
    assert temporal.binom_sum_sf([(1, 0.5), (1, 0.5)], 2) == pytest.approx(0.25)
    assert temporal.binom_sum_sf([(3, 0.2), (2, 0.5)], 1) == pytest.approx(1 - 0.8**3 * 0.5**2)
    assert temporal.binom_sum_sf([(5, 1.0)], 5) == 1.0 and temporal.binom_sum_sf([(5, 0.0)], 1) == 0.0
    assert temporal.binom_sum_sf([(400, 0.01)], 0) == pytest.approx(1.0)
    assert 0 < temporal.binom_sum_sf([(400, 0.01)], 60) < 1e-40  # deep tail stays positive
    # large n: the whole distribution counts, not the first terms of the tail
    assert temporal.binom_sum_sf([(5000, 0.5)], 1) == pytest.approx(1.0)
    assert temporal.binom_sum_sf([(5000, 0.5)], 2500) == pytest.approx(0.50564, abs=1e-4)
    assert temporal.binom_sum_sf([(3000, 0.5), (2000, 0.5)], 2500) == pytest.approx(0.50564, abs=1e-4)
    assert temporal.hypergeom_sf(2, 10, 3, 4) == pytest.approx(1 / 3)
    assert temporal.hypergeom_sf(4, 10, 3, 4) == 0.0


def test_changepoints_find_a_step_and_ignore_noise():
    assert temporal.changepoints([0.0] * 20 + [5.0] * 20) == [20]
    assert temporal.changepoints([0.0, 1.0] * 20) == []
    assert temporal.changepoints([3.0] * 40) == []


def test_regimes_split_at_rate_change():
    dates = [MONDAY + timedelta(weeks=w) for w in range(20)]
    dates += [MONDAY + timedelta(weeks=w, days=d) for w in range(20, 40) for d in range(5)]
    regimes = temporal.regimes(dates)
    assert [(r["start"], r["per_week"]) for r in regimes] == [(MONDAY, 1.0), (MONDAY + timedelta(weeks=20), 5.0)]
    assert sum(r["count"] for r in regimes) == len(dates)


def _background(days=365, every=3):
    return {f"bg{i}": MONDAY + timedelta(days=i) for i in range(0, days, every)}


def _one_regime(dated):
    return [{"start": MONDAY, "end": MONDAY + timedelta(days=365), "count": len(dated), "per_week": 0}]


def test_cohorts_find_a_burst_with_its_members():
    dated = _background()
    burst_day = MONDAY + timedelta(days=100)  # not a background day (100 % 3 != 0)
    dated |= {f"b{i}": burst_day for i in range(10)}
    cohorts = temporal.cohorts(dated, _one_regime(dated))
    assert len(cohorts) == 1
    c = cohorts[0]
    assert (c["start"], c["end"], c["size"], c["id"]) == (burst_day.isoformat(), burst_day.isoformat(), 10, 0)
    assert c["members"] == sorted(f"b{i}" for i in range(10))
    assert c["ratio"] == round(10 / (len(dated) / 365), 1)  # expected = regime total × 1 day / 365 days
    assert c["q"] < 1e-6
    assert c["label"] == "10 Apr 2024 · 10 people · ×28 usual rate"
    assert temporal.cohorts(dated, None) == temporal.cohorts(dated, temporal.regimes(list(dated.values())))


def test_cohort_dates_are_english_and_portable():
    # strftime("%-d %b") fails on Windows and follows the locale; labels must not depend on either
    assert temporal._day(date(2025, 9, 3)) == "3 Sep 2025"
    assert temporal._day(date(2025, 12, 30), year=False) == "30 Dec"
    days = {f"p{i}": date(2025, 12, 30) + timedelta(days=i % 5) for i in range(40)}
    background = {f"q{i}": date(2025, 1, 1) + timedelta(days=9 * i) for i in range(40)}
    labels = [c["label"] for c in temporal.cohorts(days | background)]
    assert any(label.startswith("30 Dec 2025–3 Jan 2026 · ") for label in labels), labels


def test_cohorts_judge_a_window_crossing_regimes_by_both_rates():
    # Slow regime (1 a week) then fast (3 a day): a 7-day window from the slow regime's last day
    # holds 13 connections, which is ordinary for the fast regime and not a burst.
    boundary = MONDAY + timedelta(days=280)
    dated = {f"a{k}": MONDAY + timedelta(days=277 - 7 * k) for k in range(40)}
    dated |= {f"b{d}-{i}": MONDAY + timedelta(days=d) for d in range(280, 350) for i in range(3)}
    regs = [
        {"start": MONDAY, "end": boundary, "count": 40},
        {"start": boundary, "end": MONDAY + timedelta(days=350), "count": 210},
    ]
    assert temporal.cohorts(dated, regs) == []


def test_cohorts_none_at_a_steady_rate():
    dated = _background()
    assert temporal.cohorts(dated, _one_regime(dated)) == []
    assert temporal.cohorts({}, []) == []
    assert temporal.cohorts({"p": MONDAY}, []) == []  # regimes derived from the dates


def test_cohort_q_counts_every_calendar_window():
    # The family is fixed in advance: 201 calendar starts x 3 window lengths, empty days included.
    dated = {"first": MONDAY} | {f"b{i}": MONDAY + timedelta(days=200) for i in range(5)}
    regs = [{"start": MONDAY, "end": MONDAY + timedelta(days=365), "count": 6}]
    (c,) = temporal.cohorts(dated, regs)
    assert c["q"] == pytest.approx(temporal.binom_sum_sf([(6, 1 / 365)], 5) * 201 * 3, rel=1e-2)


def test_cohorts_false_positive_rate_under_a_steady_rate():
    # Uniform placement in one regime: no window should be a burst more often than alpha.
    rnd = random.Random(549586)
    regs = [{"start": MONDAY, "end": MONDAY + timedelta(days=28), "count": 28}]
    hits = sum(
        bool(temporal.cohorts({f"p{i}": MONDAY + timedelta(days=rnd.randrange(28)) for i in range(28)}, regs))
        for _ in range(300)
    )
    assert hits / 300 <= 0.05


def test_cohorts_stay_finite_and_json_safe_at_a_regime_step():
    # One connection each Sunday for 20 weeks, then 20 each Monday: windows cross the step.
    sundays = {f"s{w}": MONDAY + timedelta(weeks=w, days=6) for w in range(20)}
    mondays = {f"m{w}-{i}": MONDAY + timedelta(weeks=w) for w in range(20, 40) for i in range(20)}
    cohorts = temporal.cohorts(sundays | mondays)
    json.dumps(cohorts, default=str, allow_nan=False)
    assert all(math.isfinite(c["ratio"]) for c in cohorts)


def test_coactive_pairs_beyond_chance_only():
    weeks = [MONDAY + timedelta(weeks=i) for i in range(20)]
    by_person = {"a": set(weeks[:6]), "b": set(weeks[:6]), "c": set(weeks[6:13]), "d": set(weeks[13:])}
    edges = temporal.coactive(by_person)
    assert len(edges) == 1
    assert {k: edges[0][k] for k in ("s", "t", "kind", "w", "k")} == {
        "s": "a",
        "t": "b",
        "kind": "coactive",
        "w": 1.0,
        "k": 6,
    }
    assert edges[0]["q"] < 0.01
    assert temporal.coactive({}) == []


def test_responsiveness_medians_and_starter():
    t0 = datetime(2026, 1, 1, 9, 0)
    events = [
        ("p1", "c1", t0, "out"),
        ("p1", "c1", t0 + timedelta(hours=2), "in"),
        ("p1", "c1", t0 + timedelta(hours=5), "out"),
        ("p1", "c2", t0 + timedelta(days=3), "in"),
        ("p2", "c3", t0, "out"),
    ]
    r = temporal.responsiveness(events)
    assert r["p1"] == {"n": 2, "by_them": 1, "reply_h": 2.0, "my_reply_h": 3.0}
    assert r["p2"] == {"n": 1, "by_them": 0, "reply_h": None, "my_reply_h": None}
