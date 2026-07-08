"""
Tests for recall assembly — token budget + progressive disclosure (roadmap items 3 e 6)
and the injection log. All offline: assemble_context/log_injection are pure/local.
"""
import json

import recall_session as rc


def _fact(text, conf=0.9, scope="proj"):
    return (conf, {"fact": text, "kind": "preference", "scope": scope,
                   "confidence": conf, "valid_from": "2026-01-01T00:00:00Z"})


def _row(i, project="proj"):
    return {"session_id": f"sid{i:05d}", "project": project, "machine": "mac",
            "started_at": "2026-07-01T10:00:00Z",
            "summary": f"sessao {i}: " + ("assunto detalhado " * 20)}


# ---- est_tokens ------------------------------------------------------------------

def test_est_tokens_heuristic():
    assert rc.est_tokens("") == 0
    assert rc.est_tokens("abcd") == 1
    assert rc.est_tokens("a" * 400) == 100


# ---- orcamento e divulgacao progressiva --------------------------------------------

def test_under_budget_keeps_full_previews():
    facts = [_fact("prefere commits diretos na main")]
    rows = [_row(1)]
    text, stats = rc.assemble_context("proj", facts, rows, [], max_tokens=100000)
    assert stats["style"] == "full"
    assert stats["dropped_facts"] == 0 and stats["dropped_sessions"] == 0
    assert "prefere commits" in text


def test_over_budget_switches_to_index_mode():
    facts = [_fact(f"fato numero {i} " + "x" * 40) for i in range(5)]
    rows = [_row(i) for i in range(8)]
    full_text, _ = rc.assemble_context("proj", facts, rows, [], max_tokens=100000)
    budget = rc.est_tokens(full_text) - 50  # apertado o bastante pra sair do full
    text, stats = rc.assemble_context("proj", facts, rows, [], max_tokens=budget)
    assert stats["style"] == "index"
    assert rc.est_tokens(text) <= budget


def test_way_over_budget_drops_sessions_before_facts():
    facts = [_fact(f"fato {i}") for i in range(4)]
    rows = [_row(i) for i in range(8)]
    text, stats = rc.assemble_context("proj", facts, rows, [], max_tokens=280)
    assert rc.est_tokens(text) <= 280
    # sessoes caem primeiro; fatos sao a memoria mais valiosa
    assert stats["dropped_sessions"] > 0
    assert len(stats["facts"]) >= len(stats["sessions"])


def test_budget_is_hard_cap_even_if_everything_drops():
    facts = [_fact(f"fato longo {i} " + "y" * 80) for i in range(10)]
    rows = [_row(i) for i in range(10)]
    text, stats = rc.assemble_context("proj", facts, rows, [], max_tokens=150)
    assert rc.est_tokens(text) <= 150 or (not stats["facts"] and not stats["sessions"])


def test_index_mode_points_to_mcp_for_full_detail():
    # divulgacao progressiva so funciona se o caminho pro detalhe estiver no contexto
    rows = [_row(1)]
    text, _ = rc.assemble_context("proj", [], rows, [], max_tokens=100000)
    assert "get_session" in text


# ---- log de injecao ----------------------------------------------------------------

def test_log_injection_writes_json_line(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "LOG_PATH", str(tmp_path / "recall.log"))
    stats = {"style": "full", "est_tokens": 42, "facts": ["f"], "sessions": ["s"],
             "pending": 0, "dropped_facts": 0, "dropped_sessions": 0}
    rc.log_injection("proj", "startup", stats)
    line = (tmp_path / "recall.log").read_text().strip()
    entry = json.loads(line)
    assert entry["project"] == "proj"
    assert entry["est_tokens"] == 42
    assert entry["source"] == "startup"


def test_log_injection_never_raises(monkeypatch):
    monkeypatch.setattr(rc, "LOG_PATH", "/nonexistent-dir/recall.log")
    rc.log_injection("proj", "startup", {"est_tokens": 1})  # nao pode explodir o hook
