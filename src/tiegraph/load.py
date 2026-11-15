"""Load a LinkedIn data export into the DuckDB graph.

Every edge is an observed fact from the export and gets an edge_source row.
Only the owner's 1st-degree connections become person rows: message and
invitation counterparts who are not connections are counted, not stored.
"""

import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from tiegraph.export import Export

OWNER = "me"
CONNECTIONS_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"

# Employer strings that say nothing about who knows whom: no colleague edges, no company hub.
GENERIC_ORGS = frozenset(
    {
        "stealth",
        "stealth startup",
        "stealth mode",
        "stealth mode startup",
        "stealth ai startup",
        "independent",
        "independent consultant",
        "independent projects",
        "self employed",
        "freelance",
        "freelancer",
        "confidential",
        "various",
        "none",
    }
)


def _normalize_name(name: str) -> str:
    """Lowercase, replace punctuation with spaces, collapse whitespace, drop specific tokens."""
    name = name.lower()
    name = re.sub(r"[^\w\s]", " ", name)  # Replace punctuation with spaces
    name = re.sub(r"\s+", " ", name).strip()  # Collapse whitespace
    tokens = [token for token in name.split() if token not in ("ltd", "inc", "llc", "gmbh")]
    return " ".join(tokens).strip()


def is_generic_org(name_norm: str) -> bool:
    return name_norm in GENERIC_ORGS or "stealth" in name_norm.split()


def read_aliases(path) -> dict[str, str]:
    """CSV with columns variant,canonical -> {normalized variant: canonical display name}."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {
            _normalize_name(row["variant"]): row["canonical"].strip()
            for row in csv.DictReader(f)
            if row.get("variant", "").strip() and row.get("canonical", "").strip()
        }


def init_schema(con):
    """Create the tables (idempotent): schema.sql next to this module."""
    con.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))


@contextmanager
def _transaction(con):
    con.execute("BEGIN TRANSACTION")
    try:
        yield
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _read_rows(text: str, first_column: str, name: str) -> list[dict]:
    """Rows of an export CSV; skips LinkedIn's free-text preamble before the header line."""
    # A field can be as long as its file; the csv module's default limit (128 KiB) would reject a long message
    # body that the analysis never reads.
    csv.field_size_limit(max(csv.field_size_limit(), min(len(text) + 1, 2**31 - 1)))
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if (next(csv.reader([line]), None) or [""])[0].strip() == first_column:
            return list(csv.DictReader(io.StringIO("".join(lines[i:]))))
    raise ValueError(f"{name}: header line starting with {first_column!r} not found.")


def _slug(url: str) -> str | None:
    if "/in/" not in url:
        return None
    return url.split("/in/")[1].split("?")[0].strip("/").lower() or None


def _parse_date(value: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def _org(con, company: str, aliases: dict[str, str]) -> str | None:
    """Upsert the org for a raw company string (alias-resolved) and return its id."""
    company = company.strip()
    if not company:
        return None
    display = aliases.get(_normalize_name(company), company)
    name_norm = _normalize_name(display)
    if not name_norm:
        return None
    org_id = f"org:{name_norm}"
    con.execute(
        "INSERT INTO org (id, name, name_norm) VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING",
        [org_id, display, name_norm],
    )
    return org_id


def _edge(
    con, src, dst, predicate, source, *, weight=1.0, valid_from=None, valid_to=None, note=None, refresh=False, key=""
):
    """Insert an observed edge plus its provenance row (one per edge and source).

    refresh=True overwrites weight and interval: used for aggregates such as message
    counts, where a newer export carries the full history. key separates edges that
    share src, dst and predicate (two positions at one employer).
    """
    edge_id = hashlib.sha1(f"{src}|{dst}|{predicate}{'|' + key if key else ''}".encode()).hexdigest()
    conflict = (
        ("DO UPDATE SET weight = excluded.weight, valid_from = excluded.valid_from, valid_to = excluded.valid_to")
        if refresh
        else "DO NOTHING"
    )
    con.execute(
        "INSERT INTO edge (id, src, dst, predicate, valid_from, valid_to, weight, class) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, 'observed') ON CONFLICT (id) {conflict}",
        [edge_id, src, dst, predicate, valid_from, valid_to, weight],
    )
    con.execute(
        """
        INSERT INTO edge_source (edge_id, source, observed_at, method, confidence, derived_from, note)
        SELECT ?, ?, ?, 'export', 1.0, NULL, ?
        WHERE NOT EXISTS (SELECT 1 FROM edge_source WHERE edge_id = ? AND source = ?)
        """,
        [edge_id, source, datetime.now(UTC).replace(tzinfo=None), note, edge_id, source],
    )


def _load_connections(con, text: str, source: str, aliases: dict[str, str]) -> dict:
    rows = _read_rows(text, "First Name", "Connections.csv")
    skipped = 0
    for row in rows:
        name = f"{row.get('First Name', '').strip()} {row.get('Last Name', '').strip()}".strip()
        url = row.get("URL", "").strip()
        email = row.get("Email Address", "").strip()
        position = row.get("Position", "").strip()

        if not (name or url or email):
            skipped += 1  # deactivated member: nothing identifies the row
            continue
        if "/in/" in url:
            person_id = url.split("/in/")[1].split("?")[0].strip("/")
        elif email:
            person_id = f"email:{email}"
        else:
            person_id = f"name:{_normalize_name(name)}"

        org_id = _org(con, row.get("Company", ""), aliases)
        # The latest load wins for current fields (new export, new aliases). Earlier works_at
        # edges stay as what an earlier source observed.
        con.execute(
            "INSERT INTO person (id, name, linkedin_url, email, headline, org_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, linkedin_url = excluded.linkedin_url, "
            "email = excluded.email, headline = excluded.headline, org_id = excluded.org_id",
            [person_id, name, url or None, email or None, position or None, org_id],
        )
        if org_id:
            _edge(con, person_id, org_id, "works_at", source)
        _edge(
            con,
            OWNER,
            person_id,
            "connected_to",
            source,
            valid_from=_parse_date(row.get("Connected On", ""), "%d %b %Y"),
        )
    return {"connections": len(rows), "connections_unidentified": skipped}


def _recipients(r) -> list[str]:
    return [u.strip() for u in r.get("RECIPIENT PROFILE URLS", "").split(",")]


def _owner_slugs(rows, owner_name: str | None, connections: set[str]) -> set[str]:
    """Profile slugs of the export owner in messages.csv.

    With Profile.csv the owner's name identifies their rows (several slugs survive a
    vanity-URL change). Without it, the owner is the slug in the most rows; a tie means
    the owner cannot be told apart, and the loader refuses to guess.
    """
    if owner_name:
        name = owner_name.strip().lower()
        slugs = {_slug(r.get("SENDER PROFILE URL", "")) for r in rows if r.get("FROM", "").strip().lower() == name}
        slugs |= {_slug(u) for r in rows if r.get("TO", "").strip().lower() == name for u in _recipients(r)}
        slugs -= connections | {None}  # a namesake connection is not the owner
        if slugs:
            return slugs
    counts = Counter(
        s for r in rows for s in {_slug(r.get("SENDER PROFILE URL", "")), *map(_slug, _recipients(r))} if s
    )
    top = counts.most_common(2)
    if not top:
        return set()
    if len(top) == 2 and top[0][1] == top[1][1]:
        raise ValueError("messages.csv: cannot tell which sender is you, and the export has no Profile.csv.")
    return {top[0][0]}


class _Resolver:
    """Map a counterpart (profile URL, display name) to a stored connection id."""

    def __init__(self, con):
        self.by_slug, by_name = {}, defaultdict(list)
        for person_id, name in con.execute("SELECT id, name FROM person").fetchall():
            self.by_slug[person_id.lower()] = person_id
            by_name[name.strip().lower()].append(person_id)
        self.by_name = {n: ids[0] for n, ids in by_name.items() if len(ids) == 1}

    def __call__(self, url: str, name: str) -> str | None:
        slug = _slug(url)
        if slug:  # a profile URL decides; a namesake with another profile is not this connection
            return self.by_slug.get(slug)
        return self.by_name.get(name.strip().lower())  # no URL: unique exact name only


def _load_messages(con, text: str, source: str, owner_name: str | None = None) -> dict:
    rows = [
        r
        for r in _read_rows(text, "CONVERSATION ID", "messages.csv")
        if r.get("IS MESSAGE DRAFT", "").strip().lower() != "yes"
    ]
    resolve = _Resolver(con)
    owners = _owner_slugs(rows, owner_name, set(resolve.by_slug))
    owner_lc = (owner_name or "").strip().lower()
    stats = defaultdict(lambda: {"sent": 0, "received": 0, "dates": []})
    unmatched, events = set(), []
    for r in rows:
        sender = _slug(r.get("SENDER PROFILE URL", ""))
        sent = sender in owners if sender else bool(owner_lc) and r.get("FROM", "").strip().lower() == owner_lc
        try:
            at = datetime.strptime(r.get("DATE", "").strip(), "%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            at = None
        when = at.date() if at else None
        if sent:
            urls = _recipients(r)
            names = [n.strip() for n in r.get("TO", "").split(",")]
            names += [""] * (len(urls) - len(names))
            counterparts = list(zip(urls, names, strict=False))  # names padded to urls
        else:
            counterparts = [(r.get("SENDER PROFILE URL", "").strip(), r.get("FROM", "").strip())]
        for url, name in counterparts:
            if not url and not name:
                continue  # system or sponsored message
            person_id = resolve(url, name)
            if person_id is None:
                unmatched.add(_slug(url) or name.lower())
                continue
            s = stats[person_id]
            s["sent" if sent else "received"] += 1
            if when:
                s["dates"].append(when)
                events.append((person_id, r.get("CONVERSATION ID", ""), at, "out" if sent else "in"))
    # Message metadata only (who, which thread, when, which way); never the text.
    # messages.csv carries the full history, so each load replaces the previous one.
    con.execute("DELETE FROM dm_event")
    if events:
        con.executemany("INSERT INTO dm_event VALUES (?, ?, ?, ?)", events)
    for person_id, s in stats.items():
        first, last = (min(s["dates"]), max(s["dates"])) if s["dates"] else (None, None)
        if s["sent"]:
            _edge(
                con,
                OWNER,
                person_id,
                "messaged",
                source,
                weight=s["sent"],
                valid_from=first,
                valid_to=last,
                refresh=True,
            )
        if s["received"]:
            _edge(
                con,
                person_id,
                OWNER,
                "messaged",
                source,
                weight=s["received"],
                valid_from=first,
                valid_to=last,
                refresh=True,
            )
    return {"messages": len(rows), "dm_contacts": len(stats), "dm_unmatched": len(unmatched)}


def _load_invitations(con, text: str, source: str) -> dict:
    rows = _read_rows(text, "From", "Invitations.csv")
    resolve = _Resolver(con)
    matched = unmatched = 0
    for r in rows:
        outgoing = r.get("Direction", "").strip().upper() == "OUTGOING"
        url, name = (
            (r.get("inviteeProfileUrl", ""), r.get("To", ""))
            if outgoing
            else (r.get("inviterProfileUrl", ""), r.get("From", ""))
        )
        person_id = resolve(url, name)
        if person_id is None:
            unmatched += 1  # pending or withdrawn: not a connection
            continue
        matched += 1
        src, dst = (OWNER, person_id) if outgoing else (person_id, OWNER)
        sent_at = r.get("Sent At", "").split(",")[0]
        _edge(
            con,
            src,
            dst,
            "invited",
            source,
            valid_from=_parse_date(sent_at, "%m/%d/%y"),
            note="with note" if r.get("Message", "").strip() else None,  # whether there was one, never its text
            refresh=True,
        )
    return {"invitations": len(rows), "invites_matched": matched, "invites_unmatched": unmatched}


def _month(value: str) -> date | None:
    return _parse_date(value, "%b %Y")


def _load_positions(con, text: str, source: str, aliases: dict[str, str]) -> dict:
    rows = _read_rows(text, "Company Name", "Positions.csv")
    for r in rows:
        org_id = _org(con, r.get("Company Name", ""), aliases)
        if org_id:
            started = r.get("Started On", "").strip()
            _edge(
                con,
                OWNER,
                org_id,
                "works_at",
                source,
                valid_from=_month(started),
                valid_to=_month(r.get("Finished On", "")),
                refresh=True,
                key=started,
            )
    return {"positions": len(rows)}


def _owner_name(export: Export) -> str | None:
    text = export.text("Profile.csv")
    if text is None:
        return None
    rows = _read_rows(text, "First Name", "Profile.csv")
    name = f"{rows[0].get('First Name', '').strip()} {rows[0].get('Last Name', '').strip()}".strip() if rows else ""
    return name or None


def _load_company_follows(con, text: str, source: str, aliases: dict[str, str]) -> dict:
    rows = _read_rows(text, "Organization", "Company Follows.csv")
    for r in rows:
        org_id = _org(con, r.get("Organization", ""), aliases)
        if org_id:
            followed = _parse_date(r.get("Followed On", ""), "%a %b %d %H:%M:%S %Z %Y")
            _edge(con, OWNER, org_id, "follows", source, valid_from=followed, refresh=True)
    return {"company_follows": len(rows)}


def load_export(con, export: Export, source: str | None = None, aliases: dict[str, str] | None = None) -> dict:
    """Load every file of an export that the graph uses, in one transaction.

    Connections.csv is required; messages.csv, Invitations.csv, Positions.csv,
    Company Follows.csv and Profile.csv (identifies you in messages.csv) are optional.
    """
    source = source or f"export:{export.label}"
    aliases = aliases or {}
    owner_name = _owner_name(export)
    stats = {}
    with _transaction(con):
        init_schema(con)  # idempotent; adds tables a store created by an older version lacks
        stats.update(_load_connections(con, export.text("Connections.csv"), source, aliases))
        optional = [
            ("messages.csv", lambda t: _load_messages(con, t, source, owner_name)),
            ("Invitations.csv", lambda t: _load_invitations(con, t, source)),
            ("Positions.csv", lambda t: _load_positions(con, t, source, aliases)),
            ("Company Follows.csv", lambda t: _load_company_follows(con, t, source, aliases)),
        ]
        for name, loader in optional:
            text = export.text(name)
            if text is not None:
                stats.update(loader(text))
    return stats


def check_invariants(con) -> list[str]:
    """Edge ids without a provenance row (must be empty)."""
    return [
        r[0]
        for r in con.execute(
            "SELECT e.id FROM edge e LEFT JOIN edge_source s ON s.edge_id = e.id WHERE s.edge_id IS NULL ORDER BY e.id"
        ).fetchall()
    ]
