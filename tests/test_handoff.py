"""Unit tests for the cross-tool handoff — pure parsing/formatting, plus git on a temp repo."""
import subprocess
from datetime import datetime, timezone

import handoff as ho

NOW = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)
CONTENT = ("[user]\nmigrate the login screen\n\n[assistant]\nstarted\n\n"
           "[user]\nnow add the tests\n\n[assistant]\nAdded 3 tests. Next: wire the error state.")


def _row(**kw):
    row = {"session_id": "01a121fd-7623", "tool": "codex", "machine": "work-mac", "project": "app",
           "summary": "migrate the login screen [...] now add the tests  (2q/2r)",
           "content": CONTENT, "ended_at": "2026-10-09T15:00:00+00:00", "metadata": {}}
    row.update(kw)
    return row


def test_last_turns_picks_final_user_and_assistant():
    assert ho.last_turns(CONTENT) == ("now add the tests", "Added 3 tests. Next: wire the error state.")
    assert ho.last_turns("") == ("", "")


def test_build_handoff_has_goal_last_ask_reply_and_pointer():
    text = ho.build_handoff(_row(), now=NOW)
    assert "**codex** em work-mac, há 3h (`01a121fd`)" in text
    assert "**Objetivo:** migrate the login screen" in text
    assert "**Último pedido:** now add the tests" in text
    assert "Next: wire the error state." in text
    assert "`get_session` com `01a121fd`" in text


def test_build_handoff_git_state():
    git = {"branch": "feat/login", "head": "abc1234", "dirty": ["a.swift", "b.swift"], "dirty_count": 5}
    text = ho.build_handoff(_row(metadata={"git": git}), now=NOW)
    assert "`feat/login` @ `abc1234`; 5 arquivo(s) não commitado(s): `a.swift`, `b.swift` (+3)" in text
    clean = ho.build_handoff(_row(metadata={"git": {"branch": "main", "head": "f00", "dirty": [], "dirty_count": 0}}), now=NOW)
    assert "working tree limpa" in clean


def test_build_handoff_clips_long_reply():
    text = ho.build_handoff(_row(content="[user]\nx\n\n[assistant]\n" + "y " * 2000), now=NOW)
    reply = [ln for ln in text.splitlines() if "Onde parou" in ln][0]
    assert len(reply) < ho.REPLY_CHARS + 80 and reply.endswith("…")


def test_should_inject_only_for_other_tool_or_machine_and_recent():
    row = _row()
    assert ho.should_inject(row, "claude-code", "work-mac", now=NOW)        # other tool
    assert ho.should_inject(row, "codex", "home-mac", now=NOW)              # other machine
    assert not ho.should_inject(row, "codex", "work-mac", now=NOW)          # same tool+machine
    old = _row(ended_at="2026-10-07T15:00:00+00:00")
    assert not ho.should_inject(old, "claude-code", "work-mac", now=NOW)    # older than 24h
    assert not ho.should_inject(None, "claude-code", "x", now=NOW)


def test_tool_from_payload():
    assert ho.tool_from_payload({"transcript_path": "/Users/me/.codex/sessions/2026/r.jsonl"}) == "codex"
    assert ho.tool_from_payload({"transcript_path": "/Users/me/.claude/projects/x/s.jsonl"}) == "claude-code"
    assert ho.tool_from_payload({}) == "claude-code"


def test_git_snapshot_reads_branch_head_and_dirty(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    (tmp_path / "new.txt").write_text("x")
    snap = ho.git_snapshot(str(tmp_path))
    assert snap["branch"] == "main" and len(snap["head"]) >= 7
    assert snap["dirty"] == ["new.txt"] and snap["dirty_count"] == 1


def test_git_snapshot_outside_repo_is_none(tmp_path):
    assert ho.git_snapshot(str(tmp_path)) is None
    assert ho.git_snapshot("/nonexistent/path") is None


def test_last_turns_ignores_subagents_and_system_blocks():
    content = ("[user]\nfix the flaky test please\n\n[assistant]\nFixed it; next: rerun CI.\n\n"
               "[user]\n<system-reminder>\nbackground task done\n</system-reminder>\n\n"
               "--- subagent agent-1 ---\n\n[user]\nYou are a subagent doing research\n\n"
               "[assistant]\nsubagent report")
    assert ho.last_turns(content) == ("fix the flaky test please", "Fixed it; next: rerun CI.")
