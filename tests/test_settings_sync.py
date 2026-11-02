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


# ---- scripts que viajam com o hook ---------------------------------------------------------
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "HOME", str(tmp_path))
    hooks = tmp_path / ".claude-work" / "hooks"
    hooks.mkdir(parents=True)
    return hooks


def test_hook_scripts_bundle_only_tool_hook_folders(tmp_path, monkeypatch):
    hooks = _home(tmp_path, monkeypatch)
    (hooks / "lint.py").write_text("print('ok')\n")
    os.chmod(hooks / "lint.py", 0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "other.sh").write_text("x")
    cmd = f"python3 {tmp_path}/.claude-work/hooks/lint.py && {tmp_path}/bin/other.sh >> ~/.claude-work/hooks/out.log"
    scripts, skipped = ss.hook_scripts(cmd)
    assert scripts == {"__HOME__/.claude-work/hooks/lint.py": {"content": "print('ok')\n", "exec": True}}
    assert skipped == []


def test_hook_scripts_skip_links_secrets_and_binaries(tmp_path, monkeypatch):
    hooks = _home(tmp_path, monkeypatch)
    (hooks / "real.py").write_text("x")
    os.symlink(hooks / "real.py", hooks / "link.py")
    (hooks / "secret.py").write_text("KEY = 'AKIAABCDEFGHIJKLMNOP'\n")
    (hooks / "bin").write_bytes(b"\xff\xfe\x00")
    scripts, skipped = ss.hook_scripts("~/.claude-work/hooks/link.py; ~/.claude-work/hooks/secret.py; ~/.claude-work/hooks/bin")
    assert scripts == {}
    assert sorted(r for _, r in skipped) == ["binário", "link", "parece ter segredo"]


def test_local_items_hash_unchanged_for_hooks_without_scripts(tmp_path, monkeypatch):
    hooks = _home(tmp_path, monkeypatch)
    plain = ss.local_items(_claude(["fmt x"]), "claude", "nested")
    assert all("scripts" not in f for _, f in plain.values())
    (hooks / "a.py").write_text("print(1)")
    [(kind, files)] = [v for v in ss.local_items(_claude([f"python3 {hooks}/a.py"]), "claude", "nested").values()
                       if v[0] == "hook"]
    assert list(files["scripts"]) == ["__HOME__/.claude-work/hooks/a.py"]


def test_write_scripts_writes_backs_up_and_refuses_escape(tmp_path, monkeypatch):
    hooks = _home(tmp_path, monkeypatch)
    bk = tmp_path / "bk"
    scripts = {"__HOME__/.claude-work/hooks/new/a.py": {"content": "v1", "exec": True}}
    assert ss.write_scripts(scripts, str(bk), dry=True) == list(scripts) and not (hooks / "new").exists()
    assert ss.write_scripts(scripts, str(bk)) == list(scripts)
    assert (hooks / "new" / "a.py").read_text() == "v1" and os.access(hooks / "new" / "a.py", os.X_OK)
    assert ss.write_scripts(scripts, str(bk)) == []                         # igual: não regrava
    ss.write_scripts({"__HOME__/.claude-work/hooks/new/a.py": {"content": "v2"}}, str(bk))
    assert len(os.listdir(bk)) == 1                                          # v1 foi para o backup
    for bad in ("__HOME__/.claude-work/hooks/../../evil.py", "__HOME__/bin/x.py", "/etc/x"):
        try:
            ss.write_scripts({bad: {"content": "x"}}, str(bk))
            raise AssertionError(bad)
        except ValueError:
            pass
    os.symlink(tmp_path, hooks / "out")                                      # link para fora da pasta
    try:
        ss.write_scripts({"__HOME__/.claude-work/hooks/out/x.py": {"content": "x"}}, str(bk))
        raise AssertionError("seguiu o link")
    except ValueError:
        pass


def test_missing_deps_counts_provided_scripts_as_present(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cmd = "python3 ~/.claude-work/hooks/x.py"
    assert ss.missing_deps(cmd) == ["~/.claude-work/hooks/x.py"]
    assert ss.missing_deps(cmd, provided={"__HOME__/.claude-work/hooks/x.py": {}}) == []
