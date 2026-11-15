"""Derive the analysis graph from the DuckDB store.

Observed facts come from the store. Person-to-person edges do not exist in an
export, so two kinds are inferred here and never written back:
  colleague  same non-generic organisation (confidence 0.5)
  role       TF-IDF cosine similarity of position titles, top-k per person
Communities are Louvain partitions of that contact graph; centrality is computed
on it as well, without the owner node, which would otherwise sit on every path.
Time-based structure (regimes, cohorts, co-active DM pairs, responsiveness) comes
from graph.temporal and stays out of that graph.
"""

import math
import re
from collections import Counter, defaultdict
from datetime import date

import networkx as nx

from tiegraph import temporal
from tiegraph.load import OWNER, is_generic_org

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "&",
        "|",
        "-",
        "@",
        "i",
        "ii",
        "iii",
        "my",
        "our",
        "your",
        "who",
        "we",
        "de",
        "la",
        "el",
        "en",
        "le",
        "und",
        "der",
        "die",
        "das",
    ]
)

# Ordered: the first matching domain wins. Keywords are whole words or phrases.
DOMAINS = [
    (
        "VCs & Investors",
        ["venture", "capital", "investor", "investing", "angel", "general partner", "managing partner", "vc"],
    ),
    (
        "Cyber Security",
        [
            "security",
            "ciso",
            "secops",
            "appsec",
            "cyber",
            "cybersecurity",
            "threat",
            "fraud",
            "identity",
            "palo alto networks",
        ],
    ),
    (
        "Talent & Recruiting",
        [
            "recruiter",
            "recruiting",
            "recruitment",
            "talent",
            "hr",
            "human resources",
            "staffing",
            "sourcing",
            "people partner",
        ],
    ),
    (
        "AI & Machine Learning",
        [
            "ai",
            "artificial intelligence",
            "machine learning",
            "ml",
            "mlops",
            "nlp",
            "llm",
            "llms",
            "genai",
            "deep learning",
            "data scientist",
            "agents",
            "agentic",
            "computer vision",
            "anthropic",
            "openai",
            "nvidia",
        ],
    ),
    (
        "Cloud & DevOps",
        ["devops", "sre", "platform", "infrastructure", "cloud", "observability", "kubernetes", "devex", "reliability"],
    ),
    (
        "Founders & Executives",
        [
            "founder",
            "co founder",
            "cofounder",
            "ceo",
            "cto",
            "coo",
            "cpo",
            "chief",
            "vp",
            "vice president",
            "director",
            "head",
            "president",
        ],
    ),
    (
        "Software Engineering",
        [
            "engineer",
            "engineering",
            "developer",
            "architect",
            "full stack",
            "backend",
            "frontend",
            "software",
            "programmer",
            "tech lead",
        ],
    ),
    ("Product & Design", ["product", "designer", "design", "ux", "pm"]),
    (
        "Sales & Marketing",
        [
            "sales",
            "marketing",
            "growth",
            "business development",
            "account executive",
            "customer success",
            "partnerships",
            "community",
            "devrel",
            "advocate",
        ],
    ),
    ("Research & Academia", ["research", "researcher", "professor", "phd", "student", "lecturer", "scientist"]),
]
OTHER_DOMAIN, UNKNOWN_DOMAIN = "Other", "Unknown"


SYNONYMS = [(re.compile(r"\bco[\s-]?founder"), "founder"), (re.compile(r"\bvice[\s-]president\b"), "vp")]


def tokens(text: str) -> list[str]:
    text = (text or "").lower()
    for pattern, repl in SYNONYMS:
        text = pattern.sub(repl, text)
    words = re.findall(r"[^\W_]+", text)
    return [w for w in words if w not in STOPWORDS and (len(w) > 1 or not w.isascii())]


def classify_domain(position: str, company: str) -> str:
    if not (position or company):
        return UNKNOWN_DOMAIN
    text = " " + " ".join(tokens(f"{position} {company}")) + " "
    for domain, keywords in DOMAINS:
        if any(f" {k} " in text for k in keywords):
            return domain
    return OTHER_DOMAIN


def _tfidf(docs: dict[str, list[str]]) -> dict[str, dict[str, float]]:
    df = Counter(t for toks in docs.values() for t in set(toks))
    n = len(docs)
    vectors = {}
    for key, toks in docs.items():
        tf = Counter(toks)
        vec = {t: (c / len(toks)) * (math.log((1 + n) / (1 + df[t])) + 1) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        vectors[key] = {t: v / norm for t, v in vec.items()} if norm else {}
    return vectors


def _role_edges(vectors, k: int, min_sim: float) -> dict[tuple[str, str], float]:
    """Top-k most similar titles per person, symmetric, above min_sim."""
    postings = defaultdict(list)
    for key, vec in vectors.items():
        for t, v in vec.items():
            postings[t].append((key, v))
    edges = {}
    for key, vec in vectors.items():
        scores = Counter()
        for t, v in vec.items():
            for other, w in postings[t]:
                if other != key:
                    scores[other] += v * w
        for other, sim in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]:
            if sim >= min_sim:
                pair = tuple(sorted((key, other)))
                edges[pair] = max(edges.get(pair, 0.0), round(sim, 4))
    return edges


def pagerank(G: nx.Graph, alpha: float = 0.85, tol: float = 1e-10, max_iter: int = 200) -> dict:
    """Weighted PageRank by power iteration (networkx's own needs scipy for one call)."""
    n = len(G)
    if not n:
        return {}
    out = {u: sum(d["weight"] for _, _, d in G.edges(u, data=True)) for u in G}
    rank = dict.fromkeys(G, 1.0 / n)
    for _ in range(max_iter):
        dangling = alpha * sum(rank[u] for u in G if not out[u]) / n
        new = {
            u: (1 - alpha) / n + dangling + alpha * sum(rank[v] * d["weight"] / out[v] for v, d in G[u].items())
            for u in G
        }
        if sum(abs(new[u] - rank[u]) for u in G) < n * tol:
            return new
        rank = new
    return rank


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def warmth(
    sent: int,
    received: int,
    last: date | None,
    as_of: date | None,
    invite_note: bool,
    shared_stage: bool,
    started_share: float = 0.0,
    reply_h: float | None = None,
) -> float:
    """Owner-to-contact tie strength from observed signals only."""
    recency = math.exp(-(as_of - last).days / 180) if (last and as_of) else 0.0
    return round(
        math.log1p(sent)
        + 0.7 * math.log1p(received)
        + recency
        + (0.5 if invite_note else 0.0)
        + (0.5 if shared_stage else 0.0)
        + 0.5 * started_share
        + (0.3 if reply_h is not None and reply_h <= 24 else 0.0),
        3,
    )


WARMTH_FORMULA = (
    "ln(1+sent) + 0.7*ln(1+received) + exp(-days_since_last_dm/180) "
    "+ 0.5 if invitation had a note + 0.5 if same org as one of your positions "
    "+ 0.5 * share of conversations they started + 0.3 if their median reply <= 24 h"
)


def build_model(con, *, seed: int = 42, role_k: int = 3, role_min: float = 0.35) -> dict:
    people = con.execute(
        """
        SELECT p.id, p.name, p.linkedin_url, p.headline, p.org_id, o.name, o.name_norm, e.valid_from
        FROM person p
        JOIN edge e ON e.src = ? AND e.dst = p.id AND e.predicate = 'connected_to'
        LEFT JOIN org o ON o.id = p.org_id
        ORDER BY p.id
        """,
        [OWNER],
    ).fetchall()
    dm = defaultdict(lambda: {"sent": 0, "recv": 0, "first": None, "last": None})
    for src, dst, weight, first, last in con.execute(
        "SELECT src, dst, weight, valid_from, valid_to FROM edge WHERE predicate = 'messaged'"
    ).fetchall():
        d = dm[dst if src == OWNER else src]
        d["sent" if src == OWNER else "recv"] += int(weight)
        d["first"] = min(filter(None, [d["first"], first]), default=None)
        d["last"] = max(filter(None, [d["last"], last]), default=None)
    invites = {}
    for src, dst, sent_on, note in con.execute(
        """
            SELECT e.src, e.dst, e.valid_from, max(s.note)
            FROM edge e LEFT JOIN edge_source s ON s.edge_id = e.id
            WHERE e.predicate = 'invited' GROUP BY e.src, e.dst, e.valid_from
            """
    ).fetchall():
        out = src == OWNER
        invites[dst if out else src] = {"dir": "out" if out else "in", "date": _iso(sent_on), "note": note}
    positions = con.execute(
        """
        SELECT e.dst, o.name, o.name_norm, e.valid_from, e.valid_to FROM edge e JOIN org o ON o.id = e.dst
        WHERE e.src = ? AND e.predicate = 'works_at' ORDER BY e.valid_from DESC NULLS FIRST, o.name
        """,
        [OWNER],
    ).fetchall()
    periods = defaultdict(list)  # several positions at one employer: first start .. last end
    for org_id, name, norm, f, t in positions:
        if not is_generic_org(norm):
            periods[org_id].append((name, f, t))
    stage_by_org = {
        org_id: {
            "org": ps[0][0],
            "from": _iso(min((f for _, f, _ in ps if f), default=None)),
            "to": None if any(t is None for _, _, t in ps) else _iso(max(t for _, _, t in ps)),
        }
        for org_id, ps in periods.items()
    }
    followed = {
        r[0] for r in con.execute("SELECT dst FROM edge WHERE src = ? AND predicate = 'follows'", [OWNER]).fetchall()
    }

    dates = [p[7] for p in people] + [d["last"] for d in dm.values()]
    dates += [date.fromisoformat(i["date"]) for i in invites.values() if i["date"]]
    as_of = max(filter(None, dates), default=None)

    # Time-based structure (graph.temporal).
    connected_on = {p[0]: p[7] for p in people if p[7]}
    regimes = temporal.regimes(list(connected_on.values()))
    cohorts = temporal.cohorts(connected_on, regimes)
    cohort_of = {pid: c["id"] for c in cohorts for pid in c["members"]}
    people_ids = {p[0] for p in people}
    events = [
        e
        for e in con.execute(
            "SELECT person_id, conversation_id, sent_at, direction FROM dm_event ORDER BY person_id, sent_at"
        ).fetchall()
        if e[0] in people_ids
    ]
    replies = temporal.responsiveness(events)
    weeks = defaultdict(set)
    for pid, _, at, _ in events:
        weeks[pid].add(temporal.week_of(at.date()))
    coactive_edges = temporal.coactive(dict(weeks))

    # Contact graph with inferred edges.
    G = nx.Graph()
    G.add_nodes_from(p[0] for p in people)
    by_org = defaultdict(list)
    for pid, _, _, _, org_id, _, org_norm, _ in people:
        if org_id and not is_generic_org(org_norm):
            by_org[org_id].append(pid)
    edges = []
    for org_id, members in sorted(by_org.items()):
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                G.add_edge(a, b, weight=1.0, kind="colleague")
                edges.append({"s": a, "t": b, "kind": "colleague", "w": 1.0, "org": org_id})
    vectors = _tfidf({p[0]: tokens(p[3]) for p in people if tokens(p[3])})
    for (a, b), sim in sorted(_role_edges(vectors, role_k, role_min).items()):
        if not G.has_edge(a, b):
            G.add_edge(a, b, weight=round(0.6 * sim, 4), kind="role")
            edges.append({"s": a, "t": b, "kind": "role", "w": round(sim, 3)})
    for _, _, data in G.edges(data=True):
        data["dist"] = 1.0 / data["weight"]

    parts = nx.community.louvain_communities(G, weight="weight", seed=seed) if G.number_of_edges() else []
    parts = sorted((sorted(c) for c in parts if len(c) > 1), key=lambda c: (-len(c), c[0]))
    community = {pid: i for i, members in enumerate(parts) for pid in members}
    degree = nx.degree_centrality(G) if len(G) > 1 else {}
    betweenness = nx.betweenness_centrality(G, weight="dist") if len(G) > 2 else {}
    ranks = pagerank(G)

    org_name = {p[4]: p[5] for p in people if p[4]}
    nodes = []
    for pid, name, url, headline, org_id, org, _org_norm, connected in people:
        d = dm.get(pid, {"sent": 0, "recv": 0, "first": None, "last": None})
        inv = invites.get(pid)
        stage = stage_by_org.get(org_id)
        conv = replies.get(pid)
        nodes.append(
            {
                "id": pid,
                "name": name,
                "url": url,
                "position": headline or "",
                "company": org or "",
                "org": org_id if org_id in by_org and len(by_org[org_id]) > 1 else None,
                "connected": _iso(connected),
                "domain": classify_domain(headline or "", org or ""),
                "community": community.get(pid, -1),
                "cohort": cohort_of.get(pid, -1),
                "dm": {"sent": d["sent"], "recv": d["recv"], "first": _iso(d["first"]), "last": _iso(d["last"])},
                "conv": conv,
                "invite": inv,
                "stage": stage,
                "followed": org_id in followed,
                "warmth": warmth(
                    d["sent"],
                    d["recv"],
                    d["last"],
                    as_of,
                    bool(inv and inv["note"]),
                    bool(stage),
                    conv["by_them"] / conv["n"] if conv else 0.0,
                    conv["reply_h"] if conv else None,
                ),
                "degree": round(degree.get(pid, 0.0), 5),
                "betweenness": round(betweenness.get(pid, 0.0), 5),
                "pagerank": round(ranks.get(pid, 0.0), 6),
            }
        )

    vec_sum = defaultdict(Counter)
    for pid, vec in vectors.items():
        if pid in community:
            vec_sum[community[pid]].update(vec)
    communities = []
    node_org = {n["id"]: n["org"] for n in nodes}
    for i, members in enumerate(parts):
        orgs = Counter(org_name[node_org[m]] for m in members if node_org[m])
        top_org, top_count = orgs.most_common(1)[0] if orgs else (None, 0)
        terms = [t for t, _ in vec_sum[i].most_common(3)]
        label = " · ".join(([top_org] if top_count >= max(2, len(members) * 0.3) else []) + terms) or f"Group {i + 1}"
        communities.append({"id": i, "label": label, "size": len(members)})

    return {
        "meta": {
            "as_of": _iso(as_of),
            "contacts": len(nodes),
            "edges": len(edges) + len(coactive_edges),
            "colleague_edges": sum(e["kind"] == "colleague" for e in edges),
            "role_edges": sum(e["kind"] == "role" for e in edges),
            "communities": len(parts),
            "unclustered": sum(n["community"] == -1 for n in nodes),
            "with_dm": sum(1 for n in nodes if n["dm"]["sent"] or n["dm"]["recv"]),
            "cohorts": len(cohorts),
            "in_cohorts": len(cohort_of),
            "regimes": len(regimes),
            "coactive_edges": len(coactive_edges),
            "warmth_formula": WARMTH_FORMULA,
            "inference": "colleague = same non-generic org (confidence 0.5); role = TF-IDF title "
            f"similarity >= {role_min}, top {role_k} per person. Neither is observed.",
            "temporal": "cohort = burst of new connections above its regime's rate (exact binomial scan of "
            "every 1/3/7-day window given the regime total, BH q < 0.05, at least 4 people); "
            "regime = CUSUM changepoint in weekly connections; co-active = DM weeks overlap beyond chance "
            "(hypergeometric against your active weeks, BY q < 0.1, at least 2 shared weeks).",
        },
        "owner": {"positions": [{"org": n, "from": _iso(f), "to": _iso(t)} for _, n, _, f, t in positions]},
        "nodes": nodes,
        "edges": edges + coactive_edges,
        "communities": communities,
        "cohorts": [{k: v for k, v in c.items() if k != "members"} for c in cohorts],
        "regimes": [{"start": _iso(r["start"]), "end": _iso(r["end"]), "per_week": r["per_week"]} for r in regimes],
        "domains": [d for d, _ in DOMAINS] + [OTHER_DOMAIN, UNKNOWN_DOMAIN],
        "orgs": [
            {"id": o, "name": org_name[o], "size": len(m)}
            for o, m in sorted(by_org.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            if len(m) > 1
        ],
    }
