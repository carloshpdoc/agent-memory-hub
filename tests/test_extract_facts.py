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
