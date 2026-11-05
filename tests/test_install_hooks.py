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


def test_bridge_is_its_own_hook_next_to_recall_and_capture():
    cfg = {"hooks": {"Stop": [{"matcher": "", "hooks": [dict(WANT["Stop"])]}]}}
    out, changes = ih.merge_bridge(copy.deepcopy(cfg), "codex")
    assert sorted(changes) == sorted(f"{e} ponte (novo)" for e in ih.BRIDGE_EVENTS["codex"])
    stop = [h for g in out["hooks"]["Stop"] for h in g["hooks"]]
    assert stop[0] == WANT["Stop"] and ih.BRIDGE in stop[1]["command"]        # captura intacta
    _, again = ih.merge_codex(copy.deepcopy(out), {"Stop": WANT["Stop"]})
    assert again == []                         # a ponte não é confundida com o hook de captura
    out2, changes2 = ih.merge_bridge(copy.deepcopy(out), "codex")
    assert changes2 == [] and out2 == out


def test_bridge_claude_runs_in_background_and_codex_has_timeout():
    claude, codex = ih.bridge_entry("claude"), ih.bridge_entry("codex")
    assert claude["command"].endswith("&") and "--tool claude" in claude["command"]
    assert codex["timeout"] == 3 and "--tool codex" in codex["command"] and "&" not in codex["command"]


def test_bridge_with_old_command_is_rewritten_in_place():
    old = {"type": "command", "command": f"python3 /velho/{ih.BRIDGE} --tool codex"}
    cfg = {"hooks": {"Stop": [{"matcher": "", "hooks": [old]}]}}
    out, changes = ih.merge_bridge(cfg, "codex")
    assert "Stop ponte (corrigido)" in changes
    assert out["hooks"]["Stop"] == [{"matcher": "", "hooks": [ih.bridge_entry("codex")]}]
