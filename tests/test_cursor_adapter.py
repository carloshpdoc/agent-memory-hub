"""
Tests for the Cursor adapter's pure logic — reconstructing a conversation from a
cursorDiskKV-shaped SQLite DB, plus the small time/path helpers. No network.
"""
import json
import sqlite3

import cursor as cur


def _make_db(tmp_path, composer_id, bubbles):
    """Build a minimal state.vscdb: a cursorDiskKV table with bubble rows."""
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(str(db))
    con.execute("create table cursorDiskKV (key text primary key, value text)")
    for bid, obj in bubbles:
        con.execute("insert into cursorDiskKV values (?, ?)",
                    (f"bubbleId:{composer_id}:{bid}", json.dumps(obj)))
    con.commit()
    return con, db


def test_reconstruct_orders_roles_and_resolves_project(tmp_path):
    cid = "comp-1"
    bubbles = [
        ("b1", {"type": 1, "text": "how do I run the tests",
                "createdAt": "2026-01-01T00:00:00.000Z",
                "workspaceUris": ["file:///Users/me/Development/my-proj"]}),
        ("b2", {"type": 2, "text": "run pytest",
                "createdAt": "2026-01-01T00:00:10.000Z"}),
        # a textless bubble (tool call) -> ignored in content but not fatal
        ("b3", {"type": 2, "text": "", "createdAt": "2026-01-01T00:00:20.000Z"}),
    ]
    con, _ = _make_db(tmp_path, cid, bubbles)
    headers = [{"bubbleId": "b1", "type": 1},
               {"bubbleId": "b2", "type": 2},
               {"bubbleId": "b3", "type": 2}]

    content, uts, nu, na, cwd, first, last = cur.reconstruct(con, cid, headers)
    assert nu == 1 and na == 1
    assert content == "[user]\nhow do I run the tests\n\n[assistant]\nrun pytest"
    assert uts == ["how do I run the tests"]
    assert cwd == "/Users/me/Development/my-proj"
    assert first == "2026-01-01T00:00:00.000Z"
    assert last == "2026-01-01T00:00:20.000Z"


def test_reconstruct_url_decodes_workspace(tmp_path):
    cid = "comp-2"
    bubbles = [
        ("b1", {"type": 1, "text": "hi there question",
                "workspaceUris": ["file:///Users/me/My%20Project%20(x)"]}),
    ]
    con, _ = _make_db(tmp_path, cid, bubbles)
    _, _, _, _, cwd, _, _ = cur.reconstruct(con, cid, [{"bubbleId": "b1", "type": 1}])
    assert cwd == "/Users/me/My Project (x)"


def test_reconstruct_no_workspace_leaves_cwd_none(tmp_path):
    cid = "comp-3"
    bubbles = [("b1", {"type": 1, "text": "a workspace-less quick question"})]
    con, _ = _make_db(tmp_path, cid, bubbles)
    _, _, nu, _, cwd, _, _ = cur.reconstruct(con, cid, [{"bubbleId": "b1", "type": 1}])
    assert nu == 1
    assert cwd is None  # main() turns this into project='root'


def test_ms_to_iso_roundtrips():
    iso = cur.ms_to_iso(1766380573174)
    assert iso is not None and iso.startswith("2025-12-")


def test_ms_to_iso_bad_input_is_none():
    assert cur.ms_to_iso(None) is None


def test_age_seconds_recent_is_small():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    age = cur.age_seconds(now)
    assert age is not None and age < 5


def test_age_seconds_bad_input_is_none():
    assert cur.age_seconds("not-a-date") is None


def test_cursor_db_override_respects_env(tmp_path, monkeypatch):
    db = tmp_path / "state.vscdb"
    db.write_text("")
    monkeypatch.setenv("CURSOR_DB", str(db))
    assert cur.cursor_db() == str(db)
    monkeypatch.setenv("CURSOR_DB", str(tmp_path / "missing.vscdb"))
    assert cur.cursor_db() is None
