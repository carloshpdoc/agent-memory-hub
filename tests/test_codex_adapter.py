"""
Tests for the Codex adapter skipping the hub's own sessions: extract/defrag/profile call
`codex exec`, which writes a rollout the adapter would otherwise import as a real session.
No network.
"""
import json
import subprocess

import codex
import extract_facts as ef
from capture_session import INTERNAL_PROMPT_MARKER


def _rollout(tmp_path, user_texts):
    lines = [{"type": "session_meta", "payload": {"id": "sid-1", "cwd": "/tmp/x"}}]
    for t in user_texts:
        lines.append({"type": "response_item", "timestamp": "2026-10-04T00:00:00Z",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": t}]}})
    f = tmp_path / "rollout-test.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines))
    return str(f)


def test_marked_session_is_internal(tmp_path):
    path = _rollout(tmp_path, ["<environment_context>...</environment_context>",
                               f"{INTERNAL_PROMPT_MARKER}\nCompare two facts about the same project"])
    assert codex.is_internal(codex.parse(path)[3])


def test_normal_session_is_not_internal(tmp_path):
    path = _rollout(tmp_path, ["<environment_context>...</environment_context>",
                               "fix the login bug"])
    assert not codex.is_internal(codex.parse(path)[3])


def test_marker_mentioned_mid_text_is_not_internal():
    assert not codex.is_internal([f"why does {INTERNAL_PROMPT_MARKER} show up?"])


def test_call_codex_sends_marked_prompt(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["prompt"] = cmd[-1]
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("ok")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ef.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(ef.subprocess, "run", fake_run)
    assert ef.call_codex("hello", lambda k, d=None: d) == "ok"
    assert seen["prompt"].startswith(INTERNAL_PROMPT_MARKER)
    assert codex.is_internal([seen["prompt"]])
