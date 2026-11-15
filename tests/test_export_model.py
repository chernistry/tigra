import json
import os
import re
import stat
import zipfile
from pathlib import Path

import duckdb
import pytest

from tiegraph import cli
from tiegraph.export import Export, ExportError
from tiegraph.load import check_invariants, init_schema, load_export, read_aliases
from tiegraph.model import build_model, classify_domain
from tiegraph.viz import render_html

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")

FIXTURES = Path(__file__).parent / "fixtures"
EXPORT = FIXTURES / "export"
ALIASES = FIXTURES / "org_aliases.csv"


@pytest.fixture
def con(tmp_path):
    con = duckdb.connect(database=str(tmp_path / "g.duckdb"))
    init_schema(con)
    yield con
    con.close()


@pytest.fixture
def loaded(con):
    stats = load_export(con, Export(EXPORT), "fixture", read_aliases(ALIASES))
    return con, stats


def _edges(con, predicate):
    return {
        (s, d): (w, f, t)
        for s, d, w, f, t in con.execute(
            "SELECT src, dst, weight, valid_from::VARCHAR, valid_to::VARCHAR FROM edge WHERE predicate = ?", [predicate]
        ).fetchall()
    }


def test_load_export_stats(loaded):
    _, stats = loaded
    assert stats == {
        "connections": 11,
        "connections_unidentified": 1,
        "messages": 8,
        "dm_contacts": 3,
        "dm_unmatched": 3,
        "invitations": 3,
        "invites_matched": 2,
        "invites_unmatched": 1,
        "positions": 3,
        "company_follows": 1,
    }


def test_messages_aggregate_per_direction(loaded):
    con, _ = loaded
    messaged = _edges(con, "messaged")
    assert messaged[("me", "test-person01")] == (2.0, "2026-01-10", "2026-06-01")
    assert messaged[("test-person01", "me")] == (1.0, "2026-01-10", "2026-06-01")
    assert messaged[("test-person04", "me")][0] == 1.0
    assert messaged[("me", "name:test person10")][0] == 1.0  # unique-name fallback
    assert not any("not-connected" in k for pair in messaged for k in pair)


def test_invitations_keep_direction_and_whether_there_was_a_note(loaded):
    con, _ = loaded
    assert set(_edges(con, "invited")) == {("me", "test-person02"), ("test-person05", "me")}
    notes = dict(
        con.execute(
            "SELECT e.dst, s.note FROM edge e JOIN edge_source s ON s.edge_id = e.id WHERE e.predicate = 'invited'"
        ).fetchall()
    )
    assert notes == {"test-person02": "with note", "me": None}  # the flag, never the text


def test_latest_invitation_wins_whatever_the_row_order(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    for f in EXPORT.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    inv = (d / "Invitations.csv").read_text().splitlines(keepends=True)
    extra = (
        'Test Person02,Test Owner,"8/1/25, 9:15 AM",,INCOMING,'
        "https://www.linkedin.com/in/test-person02,https://www.linkedin.com/in/test-owner\n"
    )
    results = set()
    for rows in ([*inv, extra], [inv[0], extra, *inv[1:]]):
        (d / "Invitations.csv").write_text("".join(rows))
        con = duckdb.connect(":memory:")
        load_export(con, Export(d), "fixture")
        node = next(n for n in build_model(con)["nodes"] if n["id"] == "test-person02")
        results.add(json.dumps(node["invite"], sort_keys=True))
    assert results == {json.dumps({"date": "2025-08-01", "dir": "in", "note": False}, sort_keys=True)}


def test_a_long_message_body_does_not_stop_the_import(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    for f in EXPORT.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    msgs = (d / "messages.csv").read_text().splitlines(keepends=True)
    body = "x" * 200_000  # over the csv module's default field limit of 131072
    msgs.append(
        '"c9","","Test Person01","https://www.linkedin.com/in/test-person01","Test Owner",'
        f'"https://www.linkedin.com/in/test-owner","2026-08-01 10:00:00 UTC","","{body}","INBOX","","No"\n'
    )
    (d / "messages.csv").write_text("".join(msgs))
    base = load_export(duckdb.connect(":memory:"), Export(EXPORT), "fixture")["messages"]
    con = duckdb.connect(":memory:")
    stats = load_export(con, Export(d), "fixture")
    assert stats["messages"] == base + 1
    assert body not in repr(con.execute("SELECT * FROM dm_event").fetchall())


def test_reload_is_idempotent(loaded):
    con, _ = loaded
    counts = lambda: tuple(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("edge", "edge_source"))
    events = lambda: con.execute("SELECT * FROM dm_event ORDER BY ALL").fetchall()
    before, events_before = counts(), events()
    load_export(con, Export(EXPORT), "fixture", read_aliases(ALIASES))
    after = counts()
    assert events() == events_before and len(events_before) == 5  # 5 non-draft messages with matched contacts
    assert before == after
    assert _edges(con, "messaged")[("me", "test-person01")][0] == 2.0
    assert check_invariants(con) == []


def test_colleague_edges_use_aliases_and_skip_generic(loaded):
    con, _ = loaded
    model = build_model(con)
    colleague = {tuple(sorted((e["s"], e["t"]))) for e in model["edges"] if e["kind"] == "colleague"}
    assert colleague == {
        ("test-person01", "test-person02"),
        ("test-person01", "test-person03"),
        ("test-person02", "test-person03"),
        ("test-person04", "test-person05"),
        ("test-person08", "test-person09"),
    }
    role = {tuple(sorted((e["s"], e["t"]))) for e in model["edges"] if e["kind"] == "role"}
    assert ("test-person06", "test-person07") in role  # same title family, generic employer


def test_without_aliases_variants_stay_apart(con):
    load_export(con, Export(EXPORT), "fixture")
    colleague = {tuple(sorted((e["s"], e["t"]))) for e in build_model(con)["edges"] if e["kind"] == "colleague"}
    assert ("test-person08", "test-person09") not in colleague


def test_node_fields(loaded):
    con, _ = loaded
    model = build_model(con)
    nodes = {n["id"]: n for n in model["nodes"]}
    assert model["meta"]["as_of"] == "2026-06-27"
    p1 = nodes["test-person01"]
    assert p1["dm"] == {"sent": 2, "recv": 1, "first": "2026-01-10", "last": "2026-06-01"}
    assert p1["conv"] == {"n": 1, "by_them": 0, "reply_h": 24.0, "my_reply_h": 3384.0}
    assert p1["warmth"] == pytest.approx(2.449 + 0.3, abs=0.002)  # replied within 24 h
    assert nodes["test-person04"]["conv"]["by_them"] == 1  # they wrote first
    assert nodes["test-person02"]["invite"] == {"dir": "out", "date": "2025-07-01", "note": True}
    assert nodes["test-person05"]["invite"]["dir"] == "in"
    assert nodes["test-person08"]["stage"] == {"org": "Tidewell", "from": "2024-01-01", "to": "2025-06-01"}
    assert nodes["test-person04"]["followed"] and not nodes["test-person01"]["followed"]
    assert nodes["name:test person10"]["community"] == -1
    assert nodes["name:test person10"]["domain"] == "Unknown"
    assert nodes["test-person06"]["org"] is None  # generic employer is no hub
    assert {o["name"] for o in model["orgs"]} == {"Bramble Ltd.", "Quillmark GmbH", "Tidewell"}
    assert [p["org"] for p in model["owner"]["positions"]] == ["Tidewell", "Independent projects", "Tidewell"]
    assert sum(n["pagerank"] for n in model["nodes"]) == pytest.approx(1.0, abs=1e-4)


def test_message_text_never_stored_or_rendered(loaded, tmp_path):
    con, _ = loaded
    assert [c[0] for c in con.execute("DESCRIBE dm_event").fetchall()] == [
        "person_id",
        "conversation_id",
        "sent_at",
        "direction",
    ]
    dump = repr([con.execute(f"SELECT * FROM {t}").fetchall() for (t,) in con.execute("SHOW TABLES").fetchall()])
    model = build_model(con)
    for marker in ("CONTENT-MARKER-7f3a", "SUBJECT-MARKER-91c2", "we met at the meetup"):  # DMs and invitation notes
        assert marker not in dump and marker not in json.dumps(model) and marker not in render_html(model)


def test_old_database_without_dm_event_is_migrated(tmp_path):
    con = duckdb.connect(str(tmp_path / "old.duckdb"))
    schema = (Path(__file__).parents[1] / "src" / "tiegraph" / "schema.sql").read_text()
    con.execute("".join(line for line in schema.splitlines(keepends=True) if "dm_event" not in line))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "Connections.csv").write_text("no header here\n")
    with pytest.raises(ValueError, match="header"):
        load_export(con, Export(broken), "fixture")
    assert ("dm_event",) not in con.execute("SHOW TABLES").fetchall()  # a failed import changes nothing
    load_export(con, Export(EXPORT), "fixture")
    assert con.execute("SELECT count(*) FROM dm_event").fetchone()[0] == 5
    con.close()


def test_namesake_with_other_profile_is_not_matched(loaded):
    con, _ = loaded
    assert ("test-person03", "me") not in _edges(con, "messaged")


def test_repeat_positions_at_one_employer_keep_both(loaded):
    con, _ = loaded
    spans = sorted(
        con.execute(
            "SELECT valid_from::VARCHAR, valid_to::VARCHAR FROM edge "
            "WHERE src = 'me' AND dst = 'org:tidewell' AND predicate = 'works_at'"
        ).fetchall()
    )
    assert spans == [("2024-01-01", "2024-12-01"), ("2025-03-01", "2025-06-01")]


def test_aliases_apply_when_reloading_existing_db(con):
    load_export(con, Export(EXPORT), "fixture")
    load_export(con, Export(EXPORT), "fixture", read_aliases(ALIASES))
    colleague = {tuple(sorted((e["s"], e["t"]))) for e in build_model(con)["edges"] if e["kind"] == "colleague"}
    assert ("test-person08", "test-person09") in colleague


def _single_dialog_export(tmp_path, with_profile: bool) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    (d / "Connections.csv").write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Solo,Contact,https://www.linkedin.com/in/solo-contact,,Bramble,Engineer,01 Jan 2026\n"
    )
    head = '"CONVERSATION ID","CONVERSATION TITLE","FROM","SENDER PROFILE URL","TO","RECIPIENT PROFILE URLS","DATE","SUBJECT","CONTENT","FOLDER","ATTACHMENTS","IS MESSAGE DRAFT"\n'
    (d / "messages.csv").write_text(
        head
        + '"c","","Solo Contact","https://www.linkedin.com/in/solo-contact","Test Owner","https://www.linkedin.com/in/test-owner","2026-01-02 10:00:00 UTC","","hi","INBOX","","No"\n'
        '"c","","Test Owner","https://www.linkedin.com/in/test-owner","Solo Contact","https://www.linkedin.com/in/solo-contact","2026-01-03 10:00:00 UTC","","hey","INBOX","","No"\n'
    )
    if with_profile:
        (d / "Profile.csv").write_text("First Name,Last Name,Headline\nTest,Owner,x\n")
    return d


def test_owner_from_profile_in_single_dialog(con, tmp_path):
    load_export(con, Export(_single_dialog_export(tmp_path, with_profile=True)), "fixture")
    messaged = _edges(con, "messaged")
    assert messaged[("me", "solo-contact")][0] == 1.0 and messaged[("solo-contact", "me")][0] == 1.0


def test_owner_tie_without_profile_refuses_to_guess(con, tmp_path):
    with pytest.raises(ValueError, match="which sender is you"):
        load_export(con, Export(_single_dialog_export(tmp_path, with_profile=False)), "fixture")


@POSIX
def test_write_private_replaces_world_readable_file(tmp_path):
    from tiegraph.viz import write_private

    target = tmp_path / "graph.json"
    target.write_text("old")
    target.chmod(0o644)
    write_private(target, "new")
    assert target.read_text() == "new" and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["graph.json"]  # no temp file left


def test_model_is_deterministic(loaded):
    con, _ = loaded
    assert json.dumps(build_model(con), sort_keys=True) == json.dumps(build_model(con), sort_keys=True)


@pytest.mark.parametrize(
    "position,company,domain",
    [
        ("Open Source Maintainer", "", "Other"),  # 'ai' inside a word is not AI
        ("ML Engineer", "", "AI & Machine Learning"),
        ("Talent Partner", "", "Talent & Recruiting"),
        ("Product Owner", "", "Product & Design"),
        ("", "", "Unknown"),
    ],
)
def test_classify_domain(position, company, domain):
    assert classify_domain(position, company) == domain


def _embedded(html):
    raw = re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


def test_render_html_escapes_script_breakout(loaded):
    con, _ = loaded
    model = build_model(con)
    model["nodes"][0]["name"] = "</script><img src=x onerror=alert(1)>&"
    html = render_html(model)
    assert "</script><img" not in html
    assert _embedded(html)["nodes"][0]["name"] == model["nodes"][0]["name"]


def test_template_is_offline():
    html = render_html(
        {"meta": {}, "nodes": [], "edges": [], "communities": [], "orgs": [], "domains": [], "owner": {}}
    )
    assert "default-src 'none'" in html
    assert not re.search(r'(?:src|href)\s*=\s*["\']?https?:', html)
    assert "@import" not in html and "url(" not in html


def _zip(folder: Path, target: Path, prefix: str = "") -> Path:
    with zipfile.ZipFile(target, "w") as z:
        for f in sorted(folder.iterdir()):
            z.write(f, prefix + f.name)
    return target


def test_cli_builds_from_zip_and_folder_alike(tmp_path, capsys):
    archive = _zip(EXPORT, tmp_path / "export.zip")
    assert cli.main(["build", str(archive), "--aliases", str(ALIASES), "--out", str(tmp_path / "a")]) == 0
    assert cli.main(["build", str(EXPORT), "--aliases", str(ALIASES), "--out", str(tmp_path / "b")]) == 0
    assert "10 contacts" in capsys.readouterr().out
    a, b = (tmp_path / d / "graph.json" for d in "ab")
    assert a.read_bytes() == b.read_bytes()
    assert json.loads(a.read_text())["meta"]["contacts"] == 10


def test_export_reads_a_zip_with_a_top_folder(tmp_path):
    archive = _zip(EXPORT, tmp_path / "nested.zip", prefix="Complete_Export/")
    with Export(archive) as export:
        assert export.text("connections.csv").startswith("Notes:") or "First Name" in export.text("Connections.csv")
        assert export.text("Shares.csv") is None


@pytest.mark.parametrize("make", ["missing", "not_export", "not_zip"])
def test_cli_rejects_what_is_not_an_export(tmp_path, capsys, make):
    target = tmp_path / "x.zip"
    if make == "not_export":
        with zipfile.ZipFile(target, "w") as z:
            z.writestr("notes.txt", "hello")
    elif make == "not_zip":
        target.write_text("plain text")
    assert cli.main(["build", str(target), "--out", str(tmp_path / "out")]) == 2
    assert "tiegraph:" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_db_must_be_a_new_file(tmp_path, capsys):
    db = tmp_path / "graph.duckdb"
    assert cli.main(["build", str(EXPORT), "--out", str(tmp_path / "o"), "--db", str(db)]) == 0
    assert cli.main(["build", str(EXPORT), "--out", str(tmp_path / "o"), "--db", str(db)]) == 2
    assert "already exists" in capsys.readouterr().err
    con = duckdb.connect(str(db), read_only=True)
    assert check_invariants(con) == []
    con.close()


@POSIX
def test_cli_outputs_are_private(tmp_path):
    out = tmp_path / "out"
    assert cli.main(["build", str(EXPORT), "--out", str(out), "--db", str(tmp_path / "g.duckdb")]) == 0
    for path in (tmp_path / "g.duckdb", out / "graph.json", out / "graph.html"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_export_error_names_the_problem(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ExportError, match=r"Connections\.csv"):
        Export(tmp_path / "empty")
