"""
Tests for the facts extraction helpers — parsing of LLM output and the
supersession judge parser (temporal fact invalidation, roadmap item 2).
All offline: only pure functions.
"""
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


def test_reset_how_to_builds_or_filter(monkeypatch):
    captured = {}

    def fake_http(url, headers, body=None, method="GET", timeout=60, want_headers=False):
        captured["url"] = url
        captured["body"] = body
        captured["method"] = method
        return {"content-range": "0-9/10"}

    monkeypatch.setattr(ef, "http", fake_http)
    n = ef.reset_for_reprocess("https://x", "k", "how-to")
    assert n == "10"                                  # total após a barra
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"facts_extracted_at": None}
    assert "content.ilike.*deploy*" in captured["url"]
    assert captured["url"].count("content.ilike") == len(ef.HOW_TO_TERMS)


def test_reset_all_targets_already_extracted(monkeypatch):
    captured = {}
    monkeypatch.setattr(ef, "http", lambda url, *a, **k: captured.update(url=url) or {"content-range": "*/42"})
    n = ef.reset_for_reprocess("https://x", "k", "all")
    assert n == "42"
    assert "facts_extracted_at=not.is.null" in captured["url"]


def test_main_rejects_bad_reprocess_mode():
    assert ef.main(["--reprocess", "bogus"]) == 2
