"""Unit tests for ask (mem ask) — retrieval assembly, prompt, answer parsing, args. No network."""
import json

import pytest

import ask
import extract_facts as ef


def _ctx():
    return {"sessions": [{"session_id": "aaaa1111-x", "project": "hub", "tool": "codex",
                          "started_at": "2026-10-01T10:00", "summary": "deploy do dashboard",
                          "passages": ["rode deploy.sh"]},
                         {"session_id": "bbbb2222-y", "project": None, "tool": "claude-code",
                          "started_at": "2026-09-01T10:00", "summary": "outra", "passages": ["x"]}],
            "facts": [{"kind": "procedure", "scope": "hub", "fact": "deploy via SSM"}]}


def test_retrieve_uses_best_chunks_falls_back_and_filters_facts(monkeypatch):
    monkeypatch.setattr(ask.mc, "EK", "k")
    monkeypatch.setattr(ask.mc, "embed", lambda q: [0.1])
    monkeypatch.setattr(ask.mc, "ranked_sessions", lambda q, p, k, embedding: [
        {"id": "u1", "session_id": "old", "started_at": "2026-01-01", "content": "c1"},
        {"id": "u2", "session_id": "new", "started_at": "2026-02-01", "content": "começo da sessão"}])

    def rpc(name, body):
        if name == "best_chunks":
            assert body["p_ids"] == ["u1", "u2"]
            return [{"session_fk": "u1", "text": "trecho a"}, {"session_fk": "u1", "text": "trecho b"}]
        return [{"fact": "perto", "similarity": 0.9}, {"fact": "longe", "similarity": 0.85}]
    monkeypatch.setattr(ask.mc, "rpc", rpc)
    ctx = ask.retrieve("q")
    assert [s["session_id"] for s in ctx["sessions"]] == ["new", "old"]         # mais recente primeiro
    assert ctx["sessions"][1]["passages"] == ["trecho a", "trecho b"]
    assert ctx["sessions"][0]["passages"] == ["começo da sessão"]               # sem chunks: o começo
    assert [f["fact"] for f in ctx["facts"]] == ["perto"]


def test_retrieve_requires_embed_key(monkeypatch):
    monkeypatch.setattr(ask.mc, "EK", None)
    with pytest.raises(RuntimeError):
        ask.retrieve("q")


def test_prompt_has_short_ids_facts_and_passages():
    p = ask.build_prompt("como faço o deploy?", _ctx())
    assert "[aaaa1111] hub · codex · 2026-10-01" in p and "[bbbb2222] ? ·" in p
    assert "e.g. [aaaa1111]" in p and "deploy via SSM" in p and "rode deploy.sh" in p
    assert "QUESTION: como faço o deploy?" in p


def test_parse_answer_json_prose_and_raw():
    assert ask.parse_answer('{"answer": "sim [aaaa1111]"}') == "sim [aaaa1111]"
    assert ask.parse_answer('Aqui:\n{"answer": "ok"}\n') == "ok"
    assert ask.parse_answer("texto puro") == "texto puro"


def test_sources_put_cited_first():
    lines = ask.sources(_ctx(), "veja [bbbb2222]")
    assert lines[0].startswith("* [bbbb2222]") and lines[1].startswith("  [aaaa1111]")


def test_parse_args():
    q, o = ask.parse_args(["--project", "hub", "-k", "3", "--llm", "claude", "--context", "como", "faço?"])
    assert q == "como faço?" and o == {"project": "hub", "k": 3, "llm": "claude", "context": True}
    with pytest.raises(ValueError):
        ask.parse_args(["--project"])
    with pytest.raises(ValueError):
        ask.parse_args(["-k", "0", "x"])


def test_main_without_question_or_llm(monkeypatch, capsys):
    assert ask.main([]) == 2
    monkeypatch.setattr(ask, "pick_caller", lambda g: (None, None))
    assert ask.main(["pergunta"]) == 1
    assert "FACTS_LLM" in capsys.readouterr().err


def test_main_passes_answer_schema_and_llm_override(monkeypatch, capsys):
    seen = {}

    def pick(g):
        seen["llm"] = g("FACTS_LLM")
        return "claude", lambda prompt, gg, schema=None: seen.update(schema=schema) or '{"answer": "r [aaaa1111]"}'
    monkeypatch.setattr(ask, "pick_caller", pick)
    monkeypatch.setattr(ask, "retrieve", lambda q, p, k: _ctx())
    assert ask.main(["--llm", "claude", "q"]) == 0
    assert seen == {"llm": "claude", "schema": ask.ANSWER_SCHEMA}
    out = capsys.readouterr().out
    assert out.startswith("r [aaaa1111]") and "* [aaaa1111]" in out


def test_ollama_uses_given_schema_else_facts(monkeypatch):
    sent = []
    monkeypatch.setattr(ef, "http", lambda url, h, body, m, timeout: sent.append(body["format"]) or json.dumps({"response": "{}"}))
    g = lambda k, d=None: d  # noqa: E731
    ef.call_ollama("p", g)
    ef.call_ollama("p", g, schema=ask.ANSWER_SCHEMA)
    assert sent == [ef.FACTS_SCHEMA, ask.ANSWER_SCHEMA]


def test_auto_chain_forwards_schema(monkeypatch):
    got = []
    monkeypatch.setitem(ef.PROVIDERS, "ollama", lambda p, g, **kw: got.append(kw) or "ok")
    _, caller = ef.pick_caller(lambda k, d=None: {"FACTS_LLM": "auto", "FACTS_CHAIN": "ollama"}.get(k, d))
    assert caller("p", None, schema={"x": 1}) == "ok" and got == [{"schema": {"x": 1}}]
