"""
Contract tests for the local SQLite backend: each operation must answer what its Supabase
counterpart (PostgREST filter or the RPC in sql/) answers. In-memory database, no network;
query vectors are tiny hand-made ones, so distances are easy to reason about.
"""
import json
import os

import pytest

from store import sqlite as sq


@pytest.fixture
def S(tmp_path):
    return sq.SqliteStore(path=str(tmp_path / "mem.db"), embedder=_FakeEmbedder())


class _FakeEmbedder:
    embed_key = "k"

    def __init__(self):
        self.calls = []

    def embed(self, text):
        return [1.0, 0.0, 0.0]

    def embed_texts(self, texts, **kw):
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0, 0.0] for t in texts]


def _session(S, sid, content="texto", project="p", started="2026-10-01T10:00:00Z", **kw):
    S.upsert_session({"session_id": sid, "tool": "claude-code", "machine": "m", "project": project,
                      "started_at": started, "ended_at": kw.pop("ended", started), "content": content,
                      "summary": kw.pop("summary", f"resumo {sid}"), **kw})
    return S._one("select id from sessions where session_id = ?", (sid,))["id"]


# ---- conversões ------------------------------------------------------------------------------
def test_ts_normalises_every_postgrest_shape_to_sortable_utc():
    assert sq.ts("2026-10-04T12:34:56.12345+00:00") == "2026-10-04T12:34:56.123450+00:00"
    assert sq.ts("2026-10-04T12:34:56Z") == "2026-10-04T12:34:56.000000+00:00"
    assert sq.ts("2026-10-04T09:34:56-03:00") == "2026-10-04T12:34:56.000000+00:00"
    assert sq.ts("2026-10-04") == "2026-10-04T00:00:00.000000+00:00"
    assert sq.ts("2026-10-04T12:34:56.123456789Z").endswith("56.123456+00:00")
    assert sq.ts(None) is None
    with pytest.raises(ValueError):
        sq.ts("ontem")


def test_vectors_round_trip_from_postgrest_text():
    b = sq.vec_blob("[0.5, -1, 2]")
    assert sq.blob_vec(b) == [0.5, -1.0, 2.0]
    assert sq.vec_blob([0.5, -1, 2]) == b
    with pytest.raises(ValueError):
        sq.vec_blob([])


def test_python_cosine_distance_matches_definition():
    a, b = sq.vec_blob([1, 0]), sq.vec_blob([0, 1])
    assert sq._py_cosine_distance(a, a) == pytest.approx(0.0)
    assert sq._py_cosine_distance(a, b) == pytest.approx(1.0)
    assert sq._py_cosine_distance(a, None) is None


@pytest.mark.parametrize("text,expected", [
    ("cache recall", '"cache" AND "recall"'),
    ('"hook guard" deploy', '"hook guard" AND "deploy"'),
    ("ios or android", '("ios" OR "android")'),
    ("swift -objc", '("swift") NOT "objc"'),
    ("AND NEAR( x*", '"AND" AND "NEAR(" AND "x*"'),       # nada do texto vira sintaxe do FTS5
    ("-só", None), ("   ", None), ("!!", None),
])
def test_fts_query_follows_websearch_semantics(text, expected):
    assert sq.fts_query(text) == expected


# ---- sessões e recall ------------------------------------------------------------------------
def test_upsert_session_merges_by_session_id_and_keeps_id(S):
    pk = _session(S, "s1", content="primeiro", usage={"in": 1})
    S.upsert_session({"session_id": "s1", "tool": "claude-code", "machine": "m", "content": "segundo",
                      "started_at": "2026-10-01T10:00:00Z"})
    row = S._one("select id, content, usage, summary from sessions where session_id = 's1'")
    assert row["id"] == pk and row["content"] == "segundo"
    assert row["usage"] == {"in": 1} and row["summary"] == "resumo s1"     # coluna não enviada fica
    assert S.session_ids() == {"s1"}
    assert S.fts_sessions("segundo")[0]["session_id"] == "s1"               # FTS acompanhou o update
    assert S.fts_sessions("primeiro") == []


def test_recall_bundle_shape_and_ordering(S):
    _session(S, "old", started="2026-09-01T00:00:00Z", ended="2026-10-20T00:00:00Z")
    _session(S, "new", started="2026-10-10T00:00:00Z", ended="2026-10-11T00:00:00Z")
    _session(S, "other", project="q", started="2026-10-15T00:00:00Z")
    S.insert_fact("global", "fact", None, "new", "m", [1, 0, 0])
    S.insert_fact("do p", "decision", "p", "new", "m", [1, 0, 0])
    S.insert_fact("do q", "fact", "q", "other", "m", [1, 0, 0])
    S.insert_pattern("pad", "preference", {"projects": ["p"]}, 0.9, None)
    b = S.recall_bundle("p")
    assert [r["session_id"] for r in b["proj"]] == ["new", "old"]
    assert [r["session_id"] for r in b["recent"]] == ["other", "new", "old"]
    assert sorted(f["fact"] for f in b["facts"]) == ["do p", "global"]
    assert b["pending"][0]["evidence"] == {"projects": ["p"]}
    assert b["last"][0]["session_id"] == "old"                              # maior ended_at, não started_at
    nb = S.recall_bundle(None)
    assert nb["proj"] is None and nb["last"] is None and [f["fact"] for f in nb["facts"]] == ["global"]


def test_session_tail_drops_subagent_blocks(S):
    _session(S, "s", content="conversa principal" + sq.SUBAGENT_SEP + "abc ---\nsub")
    assert S.session_tail("s") == "conversa principal"
    assert S.session_tail("s", chars=9) == "principal"
    assert S.session_tail("nao-existe") is None
    assert S.handoff_detail("s")["content"] == "conversa principal"
    assert S.session_ids_with_subagents() == {"s"}


def test_last_activity_and_find_session(S):
    _session(S, "abc-1", ended="2026-10-02T00:00:00Z")
    _session(S, "abd-2", ended="2026-10-03T00:00:00Z", tool="codex")
    S.update_session_by_id("abd-2", {"tool": "codex"})
    assert S.last_activity("p")["session_id"] == "abd-2"
    assert S.last_activity("p", exclude_tool="codex")["session_id"] == "abc-1"
    S.update_session_by_id("abc-1", {"metadata": {"cwd": "/r/wt-a"}})
    S.update_session_by_id("abd-2", {"metadata": {"cwd": "/r/wt-b"}})
    assert S.last_activity("p", cwd="/r/wt-a")["session_id"] == "abc-1"     # worktree, não o projeto
    assert S.last_activity("p", cwd="/r/outra") is None
    S.update_session_by_id("abc-1", {"metadata": {"cwd": "/r/wt-a", "git": {"branch": "fix/a"}}})
    assert S.last_activity("p", branch="fix/a")["session_id"] == "abc-1"
    assert S.last_activity("p", branch="fix/z") is None
    assert S.find_session("abc")["session_id"] == "abc-1"
    assert S.find_session("ab_") is None                                    # '_' não é curinga


# ---- fatos -----------------------------------------------------------------------------------
def test_match_facts_filters_scope_and_orders_by_distance(S):
    S.insert_fact("perto", "fact", "p", None, "m", [1, 0.1, 0])
    S.insert_fact("longe", "fact", None, None, "m", [0, 1, 0])
    S.insert_fact("outro scope", "fact", "q", None, "m", [1, 0, 0])
    old = S.insert_fact("vencido", "fact", "p", None, "m", [1, 0, 0])
    S.invalidate_fact(old, "2026-10-01T00:00:00Z")
    rows = S.match_facts([1, 0, 0], 5, "p")
    assert [r["fact"] for r in rows] == ["perto", "longe"]
    assert rows[0]["similarity"] > 0.99 and rows[1]["similarity"] == pytest.approx(0.0, abs=1e-6)
    assert len(S.match_facts([1, 0, 0], 5)) == 3                            # sem scope: todos os válidos


def test_find_fact_dupes_pairs_newer_first_within_scope(S):
    a = S.insert_fact("antigo", "fact", "p", None, "m", [1, 0, 0])
    S._conn().execute("update facts set created_at = '2026-01-01T00:00:00.000000+00:00' where id = ?", (a,))
    b = S.insert_fact("novo", "fact", "p", None, "m", [1, 0.01, 0])
    S.insert_fact("outro scope", "fact", "q", None, "m", [1, 0, 0])
    [d] = S.find_fact_dupes(0.9)
    assert (d["a_id"], d["b_id"], d["scope"]) == (b, a, "p") and d["similarity"] > 0.99


def test_stale_and_since_queries_compare_times_not_text(S):
    f = S.insert_fact("velho", "config", "p", None, "m", [1, 0, 0])
    S._conn().execute("update facts set valid_from = ? where id = ?", (sq.ts("2026-01-01T00:00:00Z"), f))
    assert [r["fact"] for r in S.stale_fact_candidates(["config"], "2026-02-01T00:00:00-03:00", 10)] == ["velho"]
    assert S.stale_fact_candidates(["fact"], "2026-02-01", 10) == []
    assert len(S.facts_created_since("2026-01-01")) == 1


def test_pending_facts_and_reset(S):
    a = _session(S, "a", content="como fazer o deploy passo a passo", started="2026-10-01T00:00:00Z")
    b = _session(S, "b", content="nada aqui", started="2026-10-02T00:00:00Z")
    assert [r["id"] for r in S.pending_fact_sessions(10)] == [b, a]
    assert [r["id"] for r in S.pending_fact_sessions(10, exclude_ids={b})] == [a]
    S.mark_facts_extracted(a, "2026-10-03T00:00:00Z")
    S.mark_facts_extracted(b, "2026-10-03T00:00:00Z")
    assert S.pending_fact_count() == 0
    assert S.reset_facts_extraction("how-to", ["Passo A Passo"]) == 1
    assert S.reset_facts_extraction("all") == 1                              # sobrou só a b extraída


# ---- chunks e busca --------------------------------------------------------------------------
def test_chunks_pending_replace_and_best_chunks(S):
    pk = _session(S, "s", content="abcdefghij")
    assert [r["id"] for r in S.pending_chunk_sessions(5)] == [pk]
    S.replace_chunks(pk, [(0, 0, 4, [1, 0, 0]), (1, 4, 8, [0, 1, 0]), (2, 8, 10, [0.9, 0.1, 0])], 10)
    assert S.pending_chunk_sessions(5) == []
    rows = S.best_chunks([1, 0, 0], [pk], per_session=2)
    assert [(r["chunk_idx"], r["text"]) for r in rows] == [(0, "abcd"), (2, "ij")]
    S.replace_chunks(pk, [(0, 0, 10, [0, 0, 1])], 10)                       # troca tudo
    assert [r["chunk_idx"] for r in S.best_chunks([1, 0, 0], [pk])] == [0]
    with pytest.raises(ValueError):
        S.replace_chunks(pk, [(0, 0, 1, [])], 10)                           # falha: nada muda
    assert len(S.best_chunks([1, 0, 0], [pk])) == 1


def test_hybrid_search_fuses_text_and_vector_ranks(S):
    a = _session(S, "a", content="configurar o cache do recall")
    b = _session(S, "b", content="assunto sem relação com a busca " + "x" * 3000)
    c = _session(S, "c", content="cache cache", project="q")
    S.replace_chunks(a, [(0, 0, 10, [0, 1, 0])], len("configurar o cache do recall"))
    S.replace_chunks(b, [(0, 0, 10, [1, 0, 0])], 3032)
    rows = S.hybrid_search("cache", [1, 0, 0], 5)
    by = {r["session_id"]: r for r in rows}
    assert set(by) == {"a", "b", "c"}
    assert by["b"]["fts_rank"] is None and by["b"]["vec_rank"] == 1
    assert by["c"]["vec_rank"] is None and by["c"]["fts_rank"] is not None
    assert by["a"]["fts_rank"] and by["a"]["vec_rank"] == 2                 # nas duas listas: maior score
    assert rows[0]["session_id"] == "a"
    assert by["a"]["score"] == pytest.approx(1 / (50 + by["a"]["fts_rank"]) + 1 / 52)
    assert len(by["b"]["content"]) == sq.HEAD_CHARS
    assert {r["session_id"] for r in S.hybrid_search("cache", [1, 0, 0], 5, project="q")} == {"c"}
    assert [r["session_id"] for r in S.hybrid_search("cache", None, 5, project="p")] == ["a"]
    assert S.hybrid_search("", None, 5) == []


def test_embed_pending_sessions_uses_the_head_of_content(S):
    _session(S, "s", content="y" * 5000)
    assert S.embed_pending_sessions(10) == {"embedded": 1, "scanned": 1}
    assert S._embedder.calls == [["y" * sq.EMBED_CHARS]]
    assert S.embed_pending_sessions(10) == {"embedded": 0, "scanned": 0}


def test_list_sessions_filters_and_text(S):
    _session(S, "a", content="deploy do dashboard", started="2026-10-01T00:00:00Z")
    _session(S, "b", content="deploy da api", project="q", started="2026-10-02T00:00:00Z")
    assert [r["session_id"] for r in S.list_sessions()] == ["b", "a"]
    assert [r["session_id"] for r in S.list_sessions("deploy", project="p")] == ["a"]
    assert [r["session_id"] for r in S.list_sessions("sessão inexistente")] == []
    assert S.list_sessions(machine="outra") == []


def test_fts_ignores_accents_and_case(S):
    _session(S, "a", content="Configuração da Sessão")
    assert S.fts_sessions("configuracao sessao")[0]["session_id"] == "a"


# ---- sync de configuração --------------------------------------------------------------------
def test_config_items_and_assignments(S):
    i1 = S.create_config_item({"kind": "hook", "name": "claude:Stop:1", "files": {"a": 1}, "hash": "h1",
                               "source_machine": "A"})
    S.create_config_item({"kind": "skill", "name": "x_y", "files": {}, "hash": "h2", "source_machine": "A"})
    assert [i["name"] for i in S.config_items(["hook"], name_prefix="claude:")] == ["claude:Stop:1"]
    assert S.config_items(name_prefix="x%") == []                           # prefixo literal
    assert S.config_item(i1)["files"] == {"a": 1}
    S.set_assignment(i1, "A", status="approved", installed_hash="h1")
    S.set_assignment(i1, "B", status="approved")
    S.set_assignment(i1, "B", note="falta dependência")                     # mescla, não apaga status
    assert S.assignments("B")[i1]["status"] == "approved"
    S.reset_other_approvals(i1, "A", "mudou")
    assert S.assignments("A")[i1]["status"] == "approved"
    assert S.assignments("B")[i1]["status"] == "pending"
    S.set_assignments([i1], "C", status="rejected")
    assert {a["machine"] for a in S.all_assignments()} == {"A", "B", "C"}
    S.update_config_item(i1, {"retired_at": "2026-10-01T00:00:00Z"})
    assert S.config_item(i1)["retired_at"].startswith("2026-10-01T00:00:00")
    with pytest.raises(ValueError):
        S.update_config_item(i1, {"name; drop table x": 1})                 # coluna fora da lista


# ---- perfil, painel e genéricas --------------------------------------------------------------
def test_patterns_ops_and_counts(S):
    S.insert_pattern("a", "preference", {}, 0.4, None)
    S.insert_pattern("b", "workflow", {}, 0.8, "regra")
    pid = S.profile_patterns(["proposed"], require_rule=True)[0]["id"]
    S.update_pattern(pid, {"status": "approved", "reviewed_at": "2026-10-01T00:00:00Z"})
    S.delete_proposed_patterns()
    assert [p["pattern"] for p in S.all_patterns()] == ["b"] and S.pattern_count("approved") == 1
    S.put_ops_status({"machine": "m", "payload": {"x": 1}})
    S.put_ops_status({"machine": "m", "payload": {"x": 2}})
    assert S.ops_statuses() == [{"machine": "m", "updated_at": S.ops_statuses()[0]["updated_at"],
                                 "payload": {"x": 2}}]


def test_usage_and_git_queries(S):
    _session(S, "a", metadata={"git": {"branch": "main"}}, usage={"t": 1})
    _session(S, "b", metadata={"git": None})
    _session(S, "c", metadata={})
    assert {r["session_id"]: r["branch"] for r in S.sessions_with_usage("2026-01-01")}["a"] == "main"
    assert S.sessions_missing_usage(["claude-code"]) == {"b": "claude-code", "c": "claude-code"}
    assert [r["session_id"] for r in S.sessions_without_git("claude-code")] == ["c"]


def test_table_rows_and_load_rows_round_trip(S, tmp_path):
    pk = _session(S, "s", content="abc")
    S.replace_chunks(pk, [(0, 0, 3, [0.5, 0.25, 0])], 3)
    dump = {t: S.table_rows(t) for t in sq.TABLE_ORDER}
    assert json.loads(dump["session_chunks"][0]["embedding"]) == [0.5, 0.25, 0.0]
    other = sq.SqliteStore(path=str(tmp_path / "copy.db"), embedder=_FakeEmbedder())
    for t in sq.TABLE_ORDER:
        other.load_rows(t, dump[t])
    assert {t: other.table_rows(t) for t in sq.TABLE_ORDER} == dump
    other.load_rows("sessions", dump["sessions"])                           # idempotente
    assert len(other.table_rows("sessions")) == 1
    assert other.fts_sessions("abc")[0]["session_id"] == "s"


def test_put_blob_stays_inside_blob_dir(S):
    S.put_blob("transcripts", "a/b.jsonl.gz", b"x")
    assert open(os.path.join(S.blob_dir(), "transcripts", "a", "b.jsonl.gz"), "rb").read() == b"x"
    for bucket, path in (("transcripts", "../../fora"), ("../x", "a")):
        with pytest.raises(ValueError):
            S.put_blob(bucket, path, b"x")


def test_get_store_selects_sqlite(monkeypatch, tmp_path):
    import store
    monkeypatch.setattr(store, "_store", None)
    monkeypatch.setenv("MEM_BACKEND", "sqlite")
    monkeypatch.setenv("MEM_DB", str(tmp_path / "x.db"))
    s = store.get_store()
    assert s.name == "sqlite" and s.path == str(tmp_path / "x.db")
    monkeypatch.setattr(store, "_store", None)


# ---- cópia entre backends (scripts/copy_store.py) --------------------------------------------
def test_copy_store_checks_coverage_and_unknown_columns(S, tmp_path):
    import copy_store
    pk = _session(S, "s", content="abc")
    S.replace_chunks(pk, [(0, 0, 3, [1, 0, 0])], 3)
    S.insert_fact("f", "fact", "p", "s", "m", [1, 0, 0])
    dst = sq.SqliteStore(path=str(tmp_path / "dst.db"), embedder=_FakeEmbedder())
    report = copy_store.copy(S, dst, log=lambda *a: None)
    assert report["sessions"] == (1, 1) and report["session_chunks"] == (1, 1) and report["facts"] == (1, 1)
    with pytest.raises(SystemExit):                                         # destino já tem dados
        copy_store.copy(S, dst, log=lambda *a: None)

    class Extra:
        def table_rows(self, table, fields="*", page=1000):
            return [{"id": "x", "coluna_nova": 1}] if table == "sessions" else []
    with pytest.raises(SystemExit, match="coluna_nova"):
        copy_store.copy(Extra(), sq.SqliteStore(path=str(tmp_path / "e.db")), log=lambda *a: None)
