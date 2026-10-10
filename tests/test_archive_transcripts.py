"""Unit tests for archive_transcripts — cleaning, exclusion and object naming (no network)."""
import archive_transcripts as at


def test_clean_strips_base64_blobs_and_redacts():
    blob = "A" * 25000
    text = '{"image":"data:image/png;base64,' + blob + '","note":"API_KEY=abcdefghijklmnop1234"}'
    out = at.clean(text)
    assert blob not in out and '"[blob removido]"' in out
    assert "abcdefghijklmnop1234" not in out and "[REDACTED:assignment]" in out


def test_clean_keeps_short_base64_like_strings():
    text = '{"sha":"' + "a" * 40 + '"}'
    assert at.clean(text) == text


def test_excluded_matches_cwd_or_project_key():
    env = {"WORKSPACE_ROOTS": "/w"}
    assert at.excluded("/w/acme-ios/src", ["acme"], env)
    assert not at.excluded("/w/personal-site", ["acme"], env)
    assert not at.excluded("/w/acme-ios", [], env)
    assert not at.excluded(None, ["acme"], env)


def test_object_path_is_safe_and_scoped():
    assert at.object_path("codex", "My Mac.local", "01a1-x") == "codex/My_Mac.local/01a1-x.jsonl.gz"
    assert at.object_path("claude-code", "m", "sid", "agent-1") == "claude-code/m/sid/agent-1.jsonl.gz"
