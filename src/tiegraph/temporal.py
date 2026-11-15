"""Time-based structure from export timestamps.

regimes         CUSUM binary segmentation of weekly connection counts
cohorts         bursts of new connections above their regime's rate: exact binomial scan
                over 1/3/7-day windows given each regime's total, Benjamini-Hochberg (1995)
coactive        contacts you messaged in the same weeks: Jaccard on weekly activity, exact
                hypergeometric test against your own active weeks, Benjamini-Yekutieli (2001)
                for dependent pairs
responsiveness  who starts conversations and how fast they reply: median reply times, since a
                self-exciting (Hawkes) model needs denser event streams than a message export has
"""

import functools
import itertools
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta


def bh(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg q-values, in input order."""
    m = len(pvalues)
    q, running = [1.0] * m, 1.0
    for rank, i in reversed(list(enumerate(sorted(range(m), key=pvalues.__getitem__), 1))):
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def by(pvalues: list[float]) -> list[float]:
    """Benjamini-Yekutieli q-values: BH scaled by the harmonic number, valid under dependence."""
    c = sum(1.0 / k for k in range(1, len(pvalues) + 1))
    return [min(1.0, v * c) for v in bh(pvalues)]


@functools.lru_cache(maxsize=256)
def _binomial(n: int, p: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """pmf of Binomial(n, p) and its upper tails, tail[k] = P(X >= k) for k = 0..n+1."""
    if p <= 0:
        pmf = (1.0,) + (0.0,) * n
    elif p >= 1:
        pmf = (0.0,) * n + (1.0,)
    else:
        c, lp, lq = math.lgamma(n + 1), math.log(p), math.log1p(-p)
        pmf = tuple(
            math.exp(c - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq) for k in range(n + 1)
        )
    tail = [0.0] * (n + 2)
    for k in range(n, -1, -1):  # summed from the far end, so small tails keep their precision
        tail[k] = tail[k + 1] + pmf[k]
    return pmf, tuple(min(1.0, t) for t in tail)


def binom_sum_sf(parts: list[tuple[int, float]], k: int) -> float:
    """P(X1 + X2 + ... >= k) for independent Xi ~ Binomial(ni, pi), exact.

    All parts but the last are convolved; the last enters through its cached upper tails,
    so one part costs a lookup and two cost O(n1).
    """
    if k <= 0:
        return 1.0
    if not parts:
        return 0.0
    dist = [1.0]
    for n, p in parts[:-1]:
        pmf = _binomial(n, p)[0]
        dist = [
            sum(dist[i] * pmf[j - i] for i in range(max(0, j - n), min(j, len(dist) - 1) + 1))
            for j in range(len(dist) + n)
        ]
    tail = _binomial(*parts[-1])[1]
    beyond = len(tail) - 1  # index of P(X >= n + 1) = 0
    return min(1.0, sum(d * tail[min(max(k - i, 0), beyond)] for i, d in enumerate(dist)))


def hypergeom_sf(k: int, N: int, K: int, n: int) -> float:
    """P(X >= k): overlap of a K-set and an n-set placed at random among N slots."""
    hi = min(K, n)
    if k > hi:
        return 0.0
    return sum(math.comb(K, x) * math.comb(N - K, n - x) for x in range(max(k, 0), hi + 1)) / math.comb(N, n)


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def changepoints(x: list[float], max_chg: int = 8, min_seg_len: int = 8, z_thr: float = 1.36) -> list[int]:
    """Indices where the series' level shifts (recursive standardized CUSUM).

    Without a shift, max|S_k|/sqrt(n) behaves like the supremum of a Brownian bridge, so the
    threshold is 1.36, its 5% critical value.
    """
    cps = []

    def rec(a: int, b: int, depth: int) -> None:
        n = b - a
        if depth >= max_chg or n < 2 * min_seg_len:
            return
        seg = x[a:b]
        mu, sd = sum(seg) / n, statistics.pstdev(seg)
        if sd <= 1e-8:
            return
        cs = best = 0.0
        k = 0
        for i, v in enumerate(seg):
            cs += (v - mu) / sd
            if abs(cs) > best:
                best, k = abs(cs), i
        if best / math.sqrt(n) < z_thr:
            return
        cp = a + k + 1
        if cp - a >= min_seg_len and b - cp >= min_seg_len:
            cps.append(cp)
            rec(a, cp, depth + 1)
            rec(cp, b, depth + 1)

    rec(0, len(x), 0)
    return sorted(set(cps))


def regimes(dates: list[date]) -> list[dict]:
    """Periods of steady weekly connection rate, split at changepoints."""
    if not dates:
        return []
    first, last = week_of(min(dates)), week_of(max(dates))
    weeks = [first + timedelta(weeks=i) for i in range((last - first).days // 7 + 1)]
    per_week = Counter(week_of(d) for d in dates)
    series = [per_week[w] for w in weeks]
    # Anscombe transform: count bursts would otherwise inflate the variance and hide shifts.
    bounds = [0, *changepoints([2 * math.sqrt(v + 3 / 8) for v in series]), len(weeks)]
    return [
        {
            "start": weeks[a],
            "end": weeks[b - 1] + timedelta(days=7),
            "count": sum(series[a:b]),
            "per_week": round(sum(series[a:b]) / (b - a), 2),
        }
        for a, b in itertools.pairwise(bounds)
    ]


def cohorts(
    dated: dict[str, date],
    regime_list: list[dict] | None = None,
    windows=(1, 3, 7),
    min_size: int = 4,
    alpha: float = 0.05,
) -> list[dict]:
    """Significant bursts of new connections, non-overlapping, members included.

    The test family is fixed before looking at the counts: every calendar day from the first
    to the last connection starts one window of each length, and all of them enter
    Benjamini-Hochberg. Given a regime's total N over D days, a steady rate puts
    Binomial(N, d/D) of them on the window's d days in it; a window crossing regimes sums
    those binomials, so each part is judged by its own regime. The test is exact, and the
    expected count is the sum of N*d/D. Without regimes, they are derived from the dates.
    """
    by_day = Counter(dated.values())
    if not by_day:
        return []
    regime_list = regime_list or regimes(list(by_day.elements()))

    def null_parts(days: list[date]) -> list[tuple[int, float]]:
        parts = []
        for r in regime_list:
            inside = sum(r["start"] <= d < r["end"] for d in days)
            if inside:
                parts.append((r["count"], inside / (r["end"] - r["start"]).days))
        return parts

    first, last = min(by_day), max(by_day)
    tests = []
    for offset in range((last - first).days + 1):
        start = first + timedelta(days=offset)
        for w in windows:
            days = [start + timedelta(days=i) for i in range(w)]
            n, parts = sum(by_day.get(d, 0) for d in days), null_parts(days)
            mu = sum(c * f for c, f in parts)
            if mu > 0:  # a window outside every regime has no baseline to test against
                tests.append({"days": days, "size": n, "expected": mu, "p": binom_sum_sf(parts, n)})
    for t, q in zip(tests, bh([t["p"] for t in tests]), strict=True):
        t["q"] = q
    chosen, taken = [], set()
    for t in sorted(
        (t for t in tests if t["q"] < alpha and t["size"] >= min_size),
        key=lambda t: (t["q"], -t["size"], t["days"][0], len(t["days"])),
    ):
        if taken.isdisjoint(t["days"]):
            taken.update(t["days"])
            chosen.append(t)
    out = []
    for t in chosen:
        members = sorted(pid for pid, d in dated.items() if d in t["days"])
        lo, hi = min(dated[m] for m in members), max(dated[m] for m in members)
        span = lo.strftime("%-d %b %Y") if lo == hi else f"{lo.strftime('%-d %b')}–{hi.strftime('%-d %b %Y')}"
        ratio = t["size"] / t["expected"]
        out.append(
            {
                "start": lo.isoformat(),
                "end": hi.isoformat(),
                "size": t["size"],
                "ratio": round(ratio, 1),
                "q": float(f"{t['q']:.3g}"),
                "members": members,
                "label": f"{span} · {t['size']} people · ×{ratio:.{1 if ratio < 10 else 0}f} usual rate",
            }
        )
    out.sort(key=lambda c: (-c["size"], c["start"]))
    for i, c in enumerate(out):
        c["id"] = i
    return out


def coactive(weeks_by_person: dict[str, set[date]], min_overlap: int = 2, alpha: float = 0.1) -> list[dict]:
    """Pairs whose DM weeks overlap more than chance allows, given your own active weeks."""
    universe = set().union(*weeks_by_person.values()) if weeks_by_person else set()
    ids = sorted(p for p, w in weeks_by_person.items() if len(w) >= min_overlap)
    tests = []
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            wa, wb = weeks_by_person[a], weeks_by_person[b]
            k = len(wa & wb)
            p = hypergeom_sf(k, len(universe), len(wa), len(wb)) if k >= min_overlap else 1.0
            tests.append((a, b, k, round(k / len(wa | wb), 3), p))
    return [
        {"s": a, "t": b, "kind": "coactive", "w": jac, "k": k, "q": float(f"{q:.3g}")}
        for (a, b, k, jac, _), q in zip(tests, by([t[4] for t in tests]), strict=True)
        if q < alpha
    ]


def responsiveness(events: list[tuple]) -> dict[str, dict]:
    """events: (person_id, conversation_id, at, direction 'out'|'in').

    by_them   conversations whose first message came from the contact
    reply_h   median hours from your message to their next message (theirs to yours: my_reply_h)
    """
    convs = defaultdict(list)
    for pid, conv, at, direction in events:
        convs[(pid, conv)].append((at, direction))
    out = defaultdict(lambda: {"n": 0, "by_them": 0, "theirs": [], "mine": []})
    for (pid, _), evs in convs.items():
        evs.sort()
        o = out[pid]
        o["n"] += 1
        o["by_them"] += evs[0][1] == "in"
        for (t0, d0), (t1, d1) in itertools.pairwise(evs):
            if d0 != d1:
                (o["theirs"] if d1 == "in" else o["mine"]).append((t1 - t0).total_seconds() / 3600)
    med = lambda xs: round(statistics.median(xs), 1) if xs else None
    return {
        pid: {"n": o["n"], "by_them": o["by_them"], "reply_h": med(o["theirs"]), "my_reply_h": med(o["mine"])}
        for pid, o in out.items()
    }
