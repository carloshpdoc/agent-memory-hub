"""scripts/app_history.py: o JSON que a aba Histórico do Agent Console lê."""
import json

import app_history as ah


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def recent_sessions(self, limit=10):
        return self.rows[:limit]

    def find_session(self, prefix):   # casa por prefixo, como os stores reais
        return next((r for r in self.rows if r["session_id"].startswith(prefix)), None)


ROWS = [
    {"session_id": "11111111-aaaa", "started_at": "2026-11-20T10:00:00Z", "machine": "mac-a", "tool": "codex",
     "project": "hub", "summary": "s", "content": "[user]\npergunta\n\n[assistant]\nresposta"},
    {"session_id": "11111111-aaaa-mais", "started_at": "2026-11-19T10:00:00Z", "machine": "mac-b",
     "tool": "claude-code", "project": "hub", "summary": "t", "content": "[user]\noutra"},
]


def run(monkeypatch, capsys, argv, rows=ROWS):
    monkeypatch.setattr(ah, "_store", lambda: FakeStore(rows))
    code = ah.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_list_keeps_only_listing_fields(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, ["list", "--limit", "1"])
    assert code == 0
    assert out["sessions"] == [{k: ROWS[0][k] for k in ("session_id", "started_at", "machine", "tool", "project", "summary")}]


def test_show_returns_turns(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, ["show", "11111111-aaaa"])
    assert code == 0
    assert out["machine"] == "mac-a"
    assert out["turns"] == [{"role": "user", "text": "pergunta"}, {"role": "agent", "text": "resposta"}]


def test_show_requires_the_whole_id_not_a_prefix(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, ["show", "11111111"], rows=ROWS[1:])
    assert code == 1 and "não está no hub" in out["error"]


def test_invalid_id_and_bad_args_are_errors_not_silence(monkeypatch, capsys, tmp_path):
    assert run(monkeypatch, capsys, ["show", "abc%"])[1]["error"].startswith("session_id inválido")
    assert "--to" in run(monkeypatch, capsys, ["pack", "11111111-aaaa", "--to", "vim", "--cwd", str(tmp_path)])[1]["error"]
    assert "--cwd" in run(monkeypatch, capsys, ["pack", "11111111-aaaa", "--to", "claude", "--cwd", "/nao/existe"])[1]["error"]
    code, out = run(monkeypatch, capsys, [])
    assert code == 2 and "uso" in out["error"]


def test_pack_names_source_and_target(monkeypatch, capsys, tmp_path):
    code, out = run(monkeypatch, capsys, ["pack", "11111111-aaaa", "--to", "claude", "--cwd", str(tmp_path)])
    assert code == 0
    assert out["pack"].startswith("# Continuação de trabalho: Codex → Claude Code")
    assert "11111111-aaaa" in out["pack"]
