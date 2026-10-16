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


def _fake_reset_http(calls, ids):
    def fake_http(url, headers, body=None, method="GET", timeout=60, want_headers=False):
        calls.append((method, url, body))
        if method == "GET":
            return json.dumps([{"id": i} for i in ids])
        return ""
    return fake_http


def test_reset_how_to_selects_ids_then_patches_in_batches(monkeypatch):
    calls, ids = [], [f"id{i}" for i in range(ef.RESET_BATCH + 5)]
    monkeypatch.setattr(ef, "http", _fake_reset_http(calls, ids))
    assert ef.reset_for_reprocess("https://x", "k", "how-to") == str(len(ids))
    get = calls[0][1]
    assert "select=id" in get and get.count("content.ilike") == len(ef.HOW_TO_TERMS)
    assert " " not in get and "content.ilike.*passo%20a%20passo*" in get
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 2 and all(b == {"facts_extracted_at": None} for _, _, b in patches)
    assert patches[0][1].count(",") == ef.RESET_BATCH - 1 and "content" not in patches[0][1]


def test_reset_all_targets_already_extracted(monkeypatch):
    calls = []
    monkeypatch.setattr(ef, "http", _fake_reset_http(calls, ["a", "b"]))
    assert ef.reset_for_reprocess("https://x", "k", "all") == "2"
    assert "facts_extracted_at=not.is.null" in calls[0][1]


def test_main_rejects_bad_reprocess_mode():
    assert ef.main(["--reprocess", "bogus"]) == 2
