"""
Tests for the defrag/reflection job (roadmap item 5) — parsing and provider
resolution. Offline: pure functions only.
"""
import defrag_facts as df
import extract_facts as ef


# ---- parse_status: default conservador ---------------------------------------------

def test_parse_status_stale():
    assert df.parse_status('{"status": "stale"}') == "stale"


def test_parse_status_keep():
    assert df.parse_status('{"status": "keep"}') == "keep"


def test_parse_status_fenced():
    assert df.parse_status('```json\n{"status": "stale"}\n```') == "stale"


def test_parse_status_garbage_defaults_keep():
    # na duvida NAO invalida — memoria e mais cara de recuperar que de guardar
    assert df.parse_status("not json") == "keep"
    assert df.parse_status("") == "keep"
    assert df.parse_status(None) == "keep"


# ---- pick_caller (resolucao FACTS_LLM/FACTS_CHAIN) ----------------------------------

def _g_from(d):
    return lambda k, default=None: d.get(k, default)


def test_pick_caller_off():
    assert ef.pick_caller(_g_from({"FACTS_LLM": "off"})) == (None, None)


def test_pick_caller_single_provider():
    name, caller = ef.pick_caller(_g_from({"FACTS_LLM": "ollama"}))
    assert name == "ollama" and callable(caller)


def test_pick_caller_auto_uses_chain_order():
    name, _ = ef.pick_caller(_g_from({"FACTS_LLM": "auto", "FACTS_CHAIN": "gemini,ollama"}))
    assert name == "gemini"


def test_pick_caller_auto_skips_unknown():
    name, _ = ef.pick_caller(_g_from({"FACTS_LLM": "auto", "FACTS_CHAIN": "nao-existe,ollama"}))
    assert name == "ollama"


def test_pick_caller_invalid_provider():
    assert ef.pick_caller(_g_from({"FACTS_LLM": "nao-existe"})) == (None, None)


# ---- escopo do sweep ----------------------------------------------------------------

def test_stale_kinds_exclude_durable_ones():
    # preferencias e decisoes nao expiram por idade — so os tipos pereciveis
    assert "preference" not in df.STALE_KINDS
    assert "decision" not in df.STALE_KINDS
    assert "config" in df.STALE_KINDS and "procedure" in df.STALE_KINDS
