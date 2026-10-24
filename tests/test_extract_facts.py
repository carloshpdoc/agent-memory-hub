"""
Tests for the facts extraction helpers — parsing of LLM output and the
supersession judge parser (temporal fact invalidation, roadmap item 2).
All offline: only pure functions.
"""
import json

import extract_facts as ef


# ---- parse_facts -----------------------------------------------------------------

def test_parse_facts_plain_array():
    out = ef.parse_facts('[{"fact": "usa uv para deps", "kind": "config", "scope": null}]')
    assert out[0]["fact"] == "usa uv para deps"


def test_parse_facts_wrapped_object_and_fences():
    txt = '```json\n{"facts": [{"fact": "prefere PT-BR", "kind": "preference"}]}\n```'
    assert ef.parse_facts(txt)[0]["kind"] == "preference"


def test_parse_facts_prose_around_json():
    txt = 'Here are the facts:\n[{"fact": "deploy via rsync", "kind": "procedure"}]\nDone!'
    out = ef.parse_facts(txt)
    assert out and out[0]["kind"] == "procedure"


def test_parse_facts_garbage_is_empty():
    assert ef.parse_facts("no json here at all") == []


def test_prompt_offers_procedure_kind():
    # o prompt de extracao deve oferecer o kind procedure (memoria procedural, item 8)
    assert '"procedure"' in ef.PROMPT


# ---- parse_relation (juiz de supersessao) ------------------------------------------

def test_parse_relation_update():
    assert ef.parse_relation('{"relation": "update"}') == "update"


def test_parse_relation_distinct():
    assert ef.parse_relation('{"relation": "distinct"}') == "distinct"


def test_parse_relation_fenced():
    assert ef.parse_relation('```json\n{"relation": "update"}\n```') == "update"


def test_parse_relation_prose_around_json():
    assert ef.parse_relation('I think:\n{"relation": "update"}\nbecause...') == "update"


def test_parse_relation_garbage_defaults_distinct():
    # default seguro: na duvida, NAO invalida o fato antigo
    assert ef.parse_relation("hmm not sure") == "distinct"
    assert ef.parse_relation("") == "distinct"
    assert ef.parse_relation(None) == "distinct"


# ---- reprocess: filtro how-to e reset (http mockado, offline) -----------------------

def test_how_to_terms_present():
    assert "deploy" in ef.HOW_TO_TERMS and "rodar" in ef.HOW_TO_TERMS


def _store_with_ids(monkeypatch, ids):
    from store.supabase import SupabaseStore
    st, calls = SupabaseStore(url="u", key="k", embed_key=""), []
    monkeypatch.setattr(st, "rest_all", lambda path: calls.append(("GET", path)) or [{"id": i} for i in ids])
    monkeypatch.setattr(st, "write", lambda path, body, **kw: calls.append(("PATCH", path, body)))
    return st, calls


def test_reset_how_to_selects_ids_then_patches_in_batches(monkeypatch):
    ids = [f"id{i}" for i in range(_batch() + 5)]
    st, calls = _store_with_ids(monkeypatch, ids)
    assert ef.reset_for_reprocess(st, "how-to") == str(len(ids))
    get = calls[0][1]
    assert "select=id" in get and get.count("content.ilike") == len(ef.HOW_TO_TERMS)
    assert " " not in get and "content.ilike.*passo%20a%20passo*" in get
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 2 and all(c[2] == {"facts_extracted_at": None} for c in patches)
    assert patches[0][1].count(",") == _batch() - 1 and "content" not in patches[0][1]


def test_reset_all_targets_already_extracted(monkeypatch):
    st, calls = _store_with_ids(monkeypatch, ["a", "b"])
    assert ef.reset_for_reprocess(st, "all") == "2"
    assert "facts_extracted_at=not.is.null" in calls[0][1]


def _batch():
    from store.supabase import SupabaseStore
    return SupabaseStore.RESET_BATCH


class _FactStore:
    """Store falso para o fluxo de extração: dedup, gravação, supersessão e marcação."""
    def __init__(self, nearest):
        self.nearest, self.inserted, self.superseded, self.marked = nearest, [], [], []

    def embed(self, text):
        return [0.0]

    def match_facts(self, vec, count, scope):
        return self.nearest

    def insert_fact(self, fact, kind, scope, sid, machine, vec):
        self.inserted.append((fact, kind, scope))
        return "new-id"

    def supersede_fact(self, old, new, when):
        self.superseded.append((old, new))

    def mark_facts_extracted(self, pk, when):
        self.marked.append(pk)


def _run_session(store, llm_out):
    caller = lambda prompt, g: llm_out if prompt.startswith("You extract") else '{"relation": "update"}'  # noqa: E731
    return ef._process_session({"id": "pk1", "session_id": "s1", "project": "hub", "content": "x"},
                               lambda k, d=None: d, [("fake", caller)], {}, store, 0.90, 0.75, {"hub"}, ())


def test_process_session_skips_duplicate_and_marks_session():
    st = _FactStore([{"id": "old", "fact": "igual", "scope": "hub", "similarity": 0.95}])
    assert _run_session(st, '[{"fact": "usar deploy.sh no hub", "kind": "procedure", "scope": "hub"}]') == (0, 0)
    assert st.inserted == [] and st.marked == ["pk1"]


def test_process_session_inserts_and_supersedes_close_update():
    st = _FactStore([{"id": "old", "fact": "deploy antigo", "scope": "hub", "similarity": 0.80}])
    assert _run_session(st, '[{"fact": "usar deploy.sh --expose no hub", "kind": "procedure", "scope": "hub"}]') == (1, 1)
    assert st.inserted == [("usar deploy.sh --expose no hub", "procedure", "hub")]
    assert st.superseded == [("old", "new-id")] and st.marked == ["pk1"]


def test_main_rejects_bad_reprocess_mode():
    assert ef.main(["--reprocess", "bogus"]) == 2
