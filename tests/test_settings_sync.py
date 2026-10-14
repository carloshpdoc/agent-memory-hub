"""Unit tests for settings_sync (config sync phase 2) — pure dict edits, identity, deps."""
import json
import os

import settings_sync as ss


def _claude(cmds=(), allow=()):
    return {"permissions": {"allow": list(allow)},
            "hooks": {"PostToolUse": [{"matcher": "Edit|Write",
                                       "hooks": [{"type": "command", "command": c} for c in cmds]}]}}


def test_hook_identity_changes_with_any_edit_and_is_home_independent(monkeypatch):
    a = ss.hook_item("claude", "PostToolUse", "Edit", f"{ss.HOME}/bin/fmt.sh")
    b = ss.hook_item("claude", "PostToolUse", "Edit", f"{ss.HOME}/bin/fmt.sh --fix")
    assert a[0] != b[0]                                          # editou -> outro item (pendente)
    assert a[1]["command"] == "__HOME__/bin/fmt.sh"              # home viaja como token
    monkeypatch.setattr(ss, "HOME", "/Users/outra-pessoa")
    assert ss.localize(a[1]["command"]) == "/Users/outra-pessoa/bin/fmt.sh"


def test_local_items_skip_hub_and_app_hooks():
    cfg = _claude(["fmt x", "python3 /x/agent-memory-hub/hooks/guard.py",
                   "\"/Users/u/Library/Application Support/gk/gk\" hook", "bash /u/.tmux/plugins/x.sh"],
                  allow=["Bash(gh:*)"])
    items = ss.local_items(cfg, "claude", "nested")
    kinds = sorted(k for k, _ in items.values())
    assert kinds == ["hook", "permission"]
    assert "claude:allow:Bash(gh:*)" in items


def test_add_and_remove_hook_nested_merge_into_matching_group():
    cfg = _claude(["a"])
    assert ss.add_hook(cfg, "nested", "PostToolUse", "Edit|Write", "b")
    assert [h["command"] for h in cfg["hooks"]["PostToolUse"][0]["hooks"]] == ["a", "b"]
    assert not ss.add_hook(cfg, "nested", "PostToolUse", "Edit|Write", "b")      # idempotente
    assert ss.add_hook(cfg, "nested", "Stop", "", "c") and cfg["hooks"]["Stop"][0]["matcher"] == ""
    assert ss.remove_hook(cfg, "nested", "Stop", "c") and "Stop" not in cfg["hooks"]
    assert ss.remove_hook(cfg, "nested", "PostToolUse", "a")
    assert [h["command"] for h in cfg["hooks"]["PostToolUse"][0]["hooks"]] == ["b"]


def test_add_and_remove_hook_flat_cursor():
    cfg = {"version": 1, "hooks": {}}
    assert ss.add_hook(cfg, "flat", "beforeShellExecution", "", "g")
    assert cfg["hooks"]["beforeShellExecution"] == [{"command": "g"}]
    assert ss.read_hooks(cfg, "flat") == [("beforeShellExecution", "", "g")]
    assert ss.remove_hook(cfg, "flat", "beforeShellExecution", "g") and cfg["hooks"] == {}


def test_permissions_add_remove():
    cfg = {}
    assert ss.add_perm(cfg, "allow", "Bash(gh:*)") and not ss.add_perm(cfg, "allow", "Bash(gh:*)")
    assert ss.read_perms(cfg, ("allow", "deny")) == [("allow", "Bash(gh:*)")]
    assert ss.remove_perm(cfg, "allow", "Bash(gh:*)") and cfg["permissions"]["allow"] == []


def test_missing_deps_ignores_redirect_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "HOME", str(tmp_path))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ok.sh").write_text("")
    cmd = "__HOME__/bin/ok.sh && __HOME__/bin/missing.sh >> __HOME__/logs/out.txt"
    assert ss.missing_deps(cmd) == ["~/bin/missing.sh"]


def test_settings_targets_dedupe_symlinked_settings(tmp_path, monkeypatch):
    main, work = tmp_path / ".claude", tmp_path / ".claude-work"
    main.mkdir(); work.mkdir()
    (main / "settings.json").write_text("{}")
    os.symlink(main / "settings.json", work / "settings.json")
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    t = ss.settings_targets("mac", [("mac", str(main / "skills"), True), ("mac:work", str(work / "skills"), False)])
    assert [x[:2] for x in t] == [("mac", "claude")]                 # uma conta, um arquivo


def test_backup_and_write_keeps_symlinks(tmp_path):
    real = tmp_path / "settings.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    os.symlink(real, link)
    ss.backup_and_write(str(link), {"a": 1}, str(tmp_path / "bk"))
    assert os.path.islink(link) and json.loads(real.read_text()) == {"a": 1}
    assert len(os.listdir(tmp_path / "bk")) == 1
