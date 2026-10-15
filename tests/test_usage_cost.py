"""Unit tests for usage (tokens per session) and cost (mem cost) — no network."""
import json

import pytest

import codex
import cost
import handoff
import usage as us


def _assistant(mid, model, **u):
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": model, "usage": u}})


def test_claude_usage_counts_each_message_once_and_adds_subagents(tmp_path):
    main = tmp_path / "s1.jsonl"
    cache = {"cache_creation": {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 0}}
    line = _assistant("m1", "claude-opus-5-5", input_tokens=2, cache_creation_input_tokens=100,
                      cache_read_input_tokens=1000, output_tokens=10, **cache)
    main.write_text("\n".join([line, line, _assistant("s", "<synthetic>", output_tokens=5),
                               json.dumps({"type": "user", "message": {"content": "oi"}})]) + "\n")
    sub = tmp_path / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(_assistant("m2", "claude-sonnet-5-5", input_tokens=1,
                                                  cache_creation_input_tokens=40, output_tokens=3) + "\n")
    u = us.claude_usage(str(main))["models"]
    assert u["claude-opus-5-5"] == {"input": 2, "cache_write_5m": 0, "cache_write_1h": 100,
                                    "cache_read": 1000, "output": 10}
    assert u["claude-sonnet-5-5"]["cache_write_5m"] == 40      # sem detalhe por TTL: tudo 5m
    assert set(u) == {"claude-opus-5-5", "claude-sonnet-5-5"}


def test_claude_usage_none_without_calls(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "oi"}}) + "\n")
    assert us.claude_usage(str(p)) is None and us.claude_usage(str(tmp_path / "nada.jsonl")) is None


def test_codex_usage_takes_last_cumulative_total(tmp_path):
    p = tmp_path / "r.jsonl"
    tc = lambda inp, cached, out: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {  # noqa: E731
        "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}})
    p.write_text("\n".join([json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.5"}}),
                            tc(100, 50, 5), tc(300, 200, 20),
                            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}})]) + "\n")
    assert us.codex_usage(str(p))["models"]["gpt-5.5"] == {
        "input": 100, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 200, "output": 20}


def test_price_longest_prefix_and_cost():
    assert us.price_for("claude-opus-5-5-20261001") == us.PRICES["claude-opus-5-5"]
    assert us.price_for("claude-opus-5") == us.PRICES["claude-opus-5"]
    assert us.price_for("gpt-5.4-mini") == us.PRICES["gpt-5.4-mini"]
    assert us.price_for("unknown-model") is None
    u = {"models": {"claude-opus-5-5": {"input": 1_000_000, "cache_write_5m": 0, "cache_write_1h": 1_000_000,
                                        "cache_read": 1_000_000, "output": 1_000_000},
                    "mystery": {"input": 5, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0}}}
    total, unpriced = us.cost_usd(u)
    assert total == pytest.approx(4 + 8 + 0.20 + 20) and unpriced == ["mystery"]
    assert us.tokens(u) == 4_000_005


def test_codex_git_from_meta():
    assert codex.git_from_meta({}) is None
    g = codex.git_from_meta({"git": {"branch": "feat/x", "commit_hash": "abcdef123456"}})
    assert g == {"branch": "feat/x", "head": "abcdef1", "dirty": [], "dirty_count": None}


def test_handoff_does_not_claim_clean_tree_when_unknown():
    row = {"tool": "codex", "machine": "m", "project": "p", "ended_at": "2026-10-14T10:00",
           "summary": "s", "content": "[user]\nfaz algo útil aqui por favor\n\n[assistant]\nfeito",
           "metadata": {"git": {"branch": "feat/x", "head": "abc1234", "dirty": [], "dirty_count": None}}}
    text = handoff.build_handoff(row)
    assert "`feat/x` @ `abc1234`" in text and "limpa" not in text


def _row(sid, project, tool, branch, cost_tokens, model="claude-opus-5-5"):
    u = {"models": {model: {"input": 0, "cache_write_5m": 0, "cache_write_1h": 0,
                            "cache_read": 0, "output": cost_tokens}}} if cost_tokens is not None else None
    return {"session_id": sid, "project": project, "tool": tool, "branch": branch,
            "started_at": "2026-10-01", "summary": "resumo", "usage": u}


def test_task_groups_feature_branch_across_tools_and_splits_default_branch():
    rows = [_row("a" * 8, "hub", "claude-code", "feat/x", 1_000_000),
            _row("b" * 8, "hub", "codex", "feat/x", 1_000_000, model="gpt-5.5"),
            _row("c" * 8, "hub", "claude-code", "main", 500_000),
            _row("d" * 8, "hub", "claude-code", None, 500_000),
            _row("e" * 8, "hub", "cursor", None, None),
            _row("f" * 8, "hub", "claude-code", None, None)]
    groups, cover, unpriced = cost.aggregate(rows, "task")
    top = groups[0]
    assert top["label"] == "hub · feat/x" and top["tools"] == {"claude-code", "codex"}
    assert top["cost"] == pytest.approx(20 + 30) and len(top["sessions"]) == 2
    assert len(groups) == 3                                    # main e sem branch: uma tarefa por sessão
    assert cover == {"with_usage": 4, "cursor": 1, "no_transcript": 1} and unpriced == []


def test_aggregate_by_model_and_tool():
    rows = [_row("a" * 8, "hub", "claude-code", None, 1_000_000), _row("b" * 8, "x", "codex", None, 1_000_000, "gpt-5.5")]
    assert [g["label"] for g in cost.aggregate(rows, "model")[0]] == ["gpt-5.5", "claude-opus-5-5"]
    assert {g["label"] for g in cost.aggregate(rows, "tool")[0]} == {"claude-code", "codex"}


def test_cost_parse_args():
    assert cost.parse_args(["--days", "7", "--by", "model"])["by"] == "model"
    for bad in (["--by", "x"], ["--days", "0"], ["--days"], ["junk"]):
        with pytest.raises(ValueError):
            cost.parse_args(bad)
