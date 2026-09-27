"""
Tests for the defrag/reflection job (roadmap item 5) — parsing and provider
resolution. Offline: pure functions only.
"""
import pytest

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


def _fake_providers(monkeypatch, behaviour, calls):
    def make(name):
        def call(prompt, g):
            calls.append(name)
            if behaviour[name] == "down":
                raise ConnectionError(name)
            return f"{name}:{prompt}"
        return call
    monkeypatch.setattr(ef, "PROVIDERS", {n: make(n) for n in behaviour})


def test_pick_caller_auto_falls_back_in_chain_order_and_sticks(monkeypatch):
    calls = []
    _fake_providers(monkeypatch, {"ollama": "down", "codex": "up", "claude": "up"}, calls)
    g = _g_from({"FACTS_LLM": "auto", "FACTS_CHAIN": "ollama,codex,claude"})
    name, caller = ef.pick_caller(g)
    assert name == "auto"
    assert caller("p1", g) == "codex:p1"
    assert caller("p2", g) == "codex:p2"
    assert calls == ["ollama", "codex", "codex"]  # the dead provider is not retried each call


def test_pick_caller_auto_skips_unknown_and_raises_when_all_down(monkeypatch):
    calls = []
    _fake_providers(monkeypatch, {"ollama": "down"}, calls)
    g = _g_from({"FACTS_LLM": "auto", "FACTS_CHAIN": "nao-existe,ollama"})
    name, caller = ef.pick_caller(g)
    assert name == "auto"
    with pytest.raises(RuntimeError, match="todos os providers falharam"):
        caller("p", g)
    assert calls == ["ollama"]


def test_pick_caller_invalid_provider():
    assert ef.pick_caller(_g_from({"FACTS_LLM": "nao-existe"})) == (None, None)


# ---- escopo do sweep ----------------------------------------------------------------

def test_stale_kinds_exclude_durable_ones():
    # preferencias e decisoes nao expiram por idade — so os tipos pereciveis
    assert "preference" not in df.STALE_KINDS
    assert "decision" not in df.STALE_KINDS
    assert "config" in df.STALE_KINDS and "procedure" in df.STALE_KINDS


# ---- consolidate: teto e memoria de pares julgados -------------------------------

def test_select_pairs_skips_judged_and_takes_most_similar_first():
    import consolidate_facts as cf
    pairs = [{"a_id": "1", "b_id": "2", "similarity": 0.86},
             {"a_id": "3", "b_id": "4", "similarity": 0.97},
             {"a_id": "5", "b_id": "6", "similarity": 0.91}]
    judged = {cf.pair_key("4", "3")}  # order-insensitive
    picked = cf.select_pairs(pairs, judged, max_pairs=1)
    assert [p["a_id"] for p in picked] == ["5"]
    assert len(cf.select_pairs(pairs, set(), max_pairs=0)) == 3


def test_judged_roundtrip(tmp_path):
    import consolidate_facts as cf
    path = tmp_path / "judged.json"
    assert cf.load_judged(str(path)) == set()
    cf.save_judged({"a|b"}, str(path))
    assert cf.load_judged(str(path)) == {"a|b"}
