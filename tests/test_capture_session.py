"""
Regression tests for the capture pipeline — pins the three silent-capture bugs so
they can't come back, plus the extractive-summary logic.

Bugs pinned:
  1. Control characters in the payload (json must parse with strict=False).
  2. NUL byte in content (Postgres text rejects it → strip_nul must remove it, nested).
  3. (shell-level echo→printf is covered indirectly: a payload full of backslashes and
     control chars must survive json.loads once it reaches Python.)
"""
import json

import capture_session as cap


# ---- Bug 1: control characters -------------------------------------------------

def test_payload_with_control_chars_needs_strict_false():
    # a raw control char (0x01) inside a JSON string value
    raw = '{"session_id": "abc", "text": "line\x01with-ctrl"}'
    # strict=True (the default) rejects it — this is exactly what used to blow up
    try:
        json.loads(raw)
        raised = False
    except json.JSONDecodeError:
        raised = True
    assert raised, "expected strict json to reject control chars"
    # strict=False (what main() now uses) parses it fine
    parsed = json.loads(raw, strict=False)
    assert parsed["text"] == "line\x01with-ctrl"


def test_payload_with_backslashes_and_ansi_survives():
    # ANSI escapes / regex / \u — the kind of thing a terminal session is full of
    payload = {"session_id": "s1", "content": "regex \\d+ ansi \x1b[31m red \x1b[0m"}
    raw = json.dumps(payload)  # valid JSON with escapes
    assert json.loads(raw, strict=False)["session_id"] == "s1"


# ---- Bug 2: NUL byte -----------------------------------------------------------

def test_strip_nul_removes_from_string():
    assert cap.strip_nul("a\x00b\x00c") == "abc"


def test_strip_nul_is_recursive():
    obj = {
        "a": "x\x00y",
        "b": ["p\x00", {"c": "q\x00r"}],
        "n": 5,
        "keep": "clean",
    }
    out = cap.strip_nul(obj)
    dumped = json.dumps(out)
    assert "\x00" not in dumped
    assert out["a"] == "xy"
    assert out["b"][0] == "p"
    assert out["b"][1]["c"] == "qr"
    assert out["n"] == 5
    assert out["keep"] == "clean"


# ---- extract_text --------------------------------------------------------------

def test_extract_text_from_plain_string():
    assert cap.extract_text("  hello  ") == "hello"


def test_extract_text_from_block_list_keeps_only_text_blocks():
    content = [
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "bash"},
        {"type": "text", "text": "second"},
    ]
    assert cap.extract_text(content) == "first\nsecond"


def test_extract_text_from_toolonly_content_is_empty():
    assert cap.extract_text([{"type": "tool_result", "content": "x"}]) == ""


# ---- clean_user_text -----------------------------------------------------------

def test_clean_user_text_strips_noise_tags():
    t = "<system-reminder>ignore me</system-reminder>\nreal question here\n<command-name>foo</command-name>"
    assert cap.clean_user_text(t) == "real question here"


# ---- build_summary -------------------------------------------------------------

def test_build_summary_has_theme_and_counts():
    s = cap.build_summary(["how do I set up the backups for this project"], 1, 3)
    assert s is not None
    assert s.endswith("(1q/3r)")
    assert "backups" in s


def test_build_summary_includes_arc_for_multiturn():
    s = cap.build_summary(
        ["first substantive question about architecture",
         "final different question about deployment"],
        2, 4,
    )
    assert "[...]" in s
    assert s.endswith("(2q/4r)")


def test_build_summary_skips_injected_context_when_picking_theme():
    s = cap.build_summary(
        ["# AGENTS.md instructions for this repo blah blah blah",
         "the actual thing I want to know about caching"],
        2, 2,
    )
    assert "AGENTS.md" not in s
    assert "caching" in s


def test_build_summary_none_when_no_substantive_text():
    assert cap.build_summary(["hi", "ok"], 2, 0) is None


# ---- parse_transcript ----------------------------------------------------------

def test_parse_transcript_counts_and_skips_textless(tmp_path):
    p = tmp_path / "session.jsonl"
    lines = [
        {"type": "user", "message": {"content": "a real question here"},
         "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "an answer"}]},
         "timestamp": "2026-01-01T00:00:05Z"},
        # tool_result with no text block -> must be skipped
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        # non-conversation entry -> ignored
        {"type": "summary", "message": {"content": "noise"}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines))

    content, n_user, n_assistant, first_ts, last_ts, user_texts = cap.parse_transcript(str(p))
    assert n_user == 1
    assert n_assistant == 1
    assert "[user]" in content and "[assistant]" in content
    assert first_ts == "2026-01-01T00:00:00Z"
    assert last_ts == "2026-01-01T00:00:05Z"
    assert user_texts == ["a real question here"]


def test_parse_transcript_missing_file_is_graceful():
    content, n_user, n_assistant, *_ = cap.parse_transcript("/nope/does-not-exist.jsonl")
    assert content == ""
    assert n_user == 0 and n_assistant == 0
