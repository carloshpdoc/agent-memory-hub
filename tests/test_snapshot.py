"""scripts/snapshot.py: merge junta sem duplicar, fecha a cobertura e não apaga nada."""
import uuid

import pytest

import snapshot
from store.sqlite import SqliteStore


def _session(store, project="work", content="[user]\noi\n\n[assistant]\nok"):
    sid = str(uuid.uuid4())
    store.upsert_session({
        "session_id": sid, "tool": "claude-code", "machine": "mac-a", "project": project,
        "started_at": "2026-11-20T10:00:00Z", "content": content, "metadata": {"a": 1},
    })
    return sid


def _store(tmp_path, name):
    s = SqliteStore(path=str(tmp_path / name))
    s._conn()  # força a conexão/schema
    return s


def test_merge_adds_new_rows_and_counts_close(tmp_path):
    src = _store(tmp_path, "snap.db")
    dst = _store(tmp_path, "dst.db")
    _session(src); _session(src)
    report = snapshot.merge_tables(src, dst)
    read, novas, ja, gravadas = report["sessions"]
    assert (read, novas, ja, gravadas) == (2, 2, 0, 2)
    assert len({r["session_id"] for r in dst.table_rows("sessions")}) == 2


def test_merge_is_idempotent_and_never_overwrites(tmp_path):
    src = _store(tmp_path, "snap.db")
    dst = _store(tmp_path, "dst.db")
    sid = _session(src, content="original")
    snapshot.merge_tables(src, dst)
    # a segunda passada não deve duplicar nem sobrescrever o que já está no destino
    report = snapshot.merge_tables(src, dst)
    assert report["sessions"] == (1, 0, 1, 0)
    rows = [r for r in dst.table_rows("sessions") if r["session_id"] == sid]
    assert len(rows) == 1 and rows[0]["content"] == "original"


def test_merge_keeps_rows_the_destination_already_had(tmp_path):
    src = _store(tmp_path, "snap.db")
    dst = _store(tmp_path, "dst.db")
    mine = _session(dst, project="personal")   # já existe só no destino
    _session(src)                              # vem do snapshot
    snapshot.merge_tables(src, dst)
    ids = {r["session_id"] for r in dst.table_rows("sessions")}
    assert mine in ids and len(ids) == 2       # nada apagado, nada perdido


def test_dry_run_writes_nothing(tmp_path):
    src = _store(tmp_path, "snap.db")
    dst = _store(tmp_path, "dst.db")
    _session(src)
    report = snapshot.merge_tables(src, dst, dry=True)
    assert report["sessions"] == (1, 1, 0, 0)
    assert dst.table_rows("sessions") == []


def test_consistent_copy_matches_row_counts(tmp_path):
    src = _store(tmp_path, "live.db")
    _session(src); _session(src)
    counts = snapshot._consistent_copy(str(tmp_path / "live.db"), str(tmp_path / "copy.db"))
    assert counts["sessions"] == 2
    copy = SqliteStore(path=str(tmp_path / "copy.db"))
    assert len(copy.table_rows("sessions")) == 2


def test_export_then_merge_roundtrip_with_age(tmp_path, monkeypatch):
    """Ciclo real: export cifra com age, merge decifra e junta. Pula se age não existe."""
    import shutil, subprocess, uuid as _uuid
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("age não instalado")
    ident = tmp_path / "id.txt"
    subprocess.run(["age-keygen", "-o", str(ident)], capture_output=True, check=True)
    recipient = next(l.split(": ", 1)[1].strip()
                     for l in ident.read_text().splitlines() if l.startswith("# public key:"))

    origem = _store(tmp_path, "origem.db")
    sid = _session(origem)
    monkeypatch.setattr(snapshot, "get_store", lambda: origem)
    out = tmp_path / "snap.age"
    rc = snapshot.main(["export", "--to", str(out), "--recipient", recipient])
    assert rc == 0 and out.exists() and out.stat().st_size > 0

    destino = _store(tmp_path, "destino.db")
    monkeypatch.setattr(snapshot, "get_store", lambda: destino)
    rc = snapshot.main(["merge", str(out), "--identity", str(ident)])
    assert rc == 0
    assert sid in {r["session_id"] for r in destino.table_rows("sessions")}
