"""Unit tests for the Codex part of install_hooks — pure config merge, no filesystem."""
import copy

import install_hooks as ih

HUB = "/x/" + ih.MARKER
WANT = {"SessionStart": {"type": "command", "command": f"python3 {HUB}recall_session.py", "timeout": 15},
        "Stop": {"type": "command", "command": f"... {HUB}codex_capture.py &"}}


def test_merge_codex_fixes_wrong_hub_hook_in_place_and_keeps_others():
    cfg = {"hooks": {"Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": f"... {HUB}capture_session.py &"},
        {"type": "command", "command": "gk ai hook run"}]}]}}
    out, changes = ih.merge_codex(copy.deepcopy(cfg), {"Stop": WANT["Stop"]})
    hooks = out["hooks"]["Stop"][0]["hooks"]
    assert hooks[0] == WANT["Stop"]                 # same slot, new command
    assert hooks[1] == {"type": "command", "command": "gk ai hook run"}
    assert changes == ["Stop (corrigido)"]


def test_merge_codex_appends_missing_and_is_idempotent():
    out, changes = ih.merge_codex({}, WANT)
    assert sorted(changes) == ["SessionStart (novo)", "Stop (novo)"]
    again, changes2 = ih.merge_codex(copy.deepcopy(out), WANT)
    assert changes2 == [] and again == out


def test_merge_codex_leaves_correct_hook_untouched():
    cfg = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [dict(WANT["SessionStart"])]}]}}
    _, changes = ih.merge_codex(copy.deepcopy(cfg), {"SessionStart": WANT["SessionStart"]})
    assert changes == []
