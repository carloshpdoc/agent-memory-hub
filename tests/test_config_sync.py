"""Unit tests for config_sync — the per-skill decision table, safety checks and install."""
import os

import pytest

import config_sync as cs


@pytest.fixture(autouse=True)
def no_real_home_dirs(monkeypatch):
    """Nenhum teste pode detectar/escrever nas pastas ~/.claude-* reais."""
    monkeypatch.setenv("CLAUDE_EXTRA_SKILL_DIRS", "")

OLD, NEW = "2026-10-01T00:00:00+00:00", "2026-10-14T00:00:00+00:00"


def _loc(h, mtime=0.0, link=False):
    return {"hash": h, "mtime": mtime, "link": link, "files": {"SKILL.md": h}}


def _item(h, updated=OLD):
    return {"id": "i1", "hash": h, "updated_at": updated, "files": {"SKILL.md": h}}


def test_decide_publish_update_and_noop():
    assert cs.decide(_loc("a"), None, None) == "publish"
    assert cs.decide(_loc("a"), _item("a"), {"status": "approved", "installed_hash": "a"}) == "noop"
    # mudou só aqui -> sobe
    assert cs.decide(_loc("b"), _item("a"), {"status": "approved", "installed_hash": "a"}) == "update"


def test_decide_install_only_when_approved():
    assert cs.decide(None, _item("a"), {"status": "approved", "installed_hash": None}) == "install"
    assert cs.decide(None, _item("a"), {"status": "pending"}) == "noop"
    assert cs.decide(None, _item("a"), None) == "noop"
    # mudou só no hub -> baixa, se aprovada
    assert cs.decide(_loc("a"), _item("b"), {"status": "approved", "installed_hash": "a"}) == "install"
    assert cs.decide(_loc("a"), _item("b"), {"status": "pending", "installed_hash": "a"}) == "noop"


def test_decide_conflict_newest_wins():
    a = {"status": "approved", "installed_hash": "a"}
    newer_local = _loc("b", mtime=2e9)          # 2033: mais novo que o hub
    assert cs.decide(newer_local, _item("c", updated=NEW), a) == "conflict-local"
    older_local = _loc("b", mtime=1.0)
    assert cs.decide(older_local, _item("c", updated=NEW), a) == "conflict-remote"


def test_decide_rejected_uninstalls_only_sync_copies():
    rej = {"status": "rejected", "installed_hash": "a"}
    assert cs.decide(_loc("a"), _item("a"), rej) == "uninstall"
    assert cs.decide(_loc("a", link=True), _item("a"), rej) == "noop"          # link de repo: intocado
    assert cs.decide(_loc("a"), _item("a"), {"status": "rejected", "installed_hash": None}) == "noop"


def test_safe_relpath_blocks_traversal():
    assert cs.safe_relpath("SKILL.md") and cs.safe_relpath("scripts/run.py")
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        assert not cs.safe_relpath(bad), bad


def test_write_skill_installs_backs_up_and_links(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SKILLS_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(cs, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(cs, "BACKUP_DIR", str(tmp_path / "bk"))
    assert cs.write_skill("demo", {"SKILL.md": "v1", "scripts/a.py": "x"}) == "instalada"
    assert (tmp_path / "claude" / "demo" / "scripts" / "a.py").read_text() == "x"
    assert os.path.realpath(tmp_path / "agents" / "demo") == str(tmp_path / "claude" / "demo")
    cs.write_skill("demo", {"SKILL.md": "v2"})
    assert (tmp_path / "claude" / "demo" / "SKILL.md").read_text() == "v2"
    assert len(os.listdir(tmp_path / "bk")) == 1                                # v1 em backup


def test_link_into_replaces_stale_copy_with_link(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SKILLS_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(cs, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(cs, "BACKUP_DIR", str(tmp_path / "bk"))
    (tmp_path / "claude" / "s").mkdir(parents=True)
    (tmp_path / "agents" / "s").mkdir(parents=True)
    (tmp_path / "agents" / "s" / "SKILL.md").write_text("velha")
    assert cs.link_into(cs.AGENTS_DIR, "s")                         # retornou o backup
    assert os.path.islink(tmp_path / "agents" / "s")
    assert len(os.listdir(tmp_path / "bk")) == 1
    assert cs.link_into(cs.AGENTS_DIR, "s") is None                 # já é link: nada a fazer


def test_targets_name_each_claude_config_as_its_own_target(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SKILLS_DIR", str(tmp_path / ".claude" / "skills"))
    monkeypatch.setenv("CLAUDE_EXTRA_SKILL_DIRS", str(tmp_path / ".claude-work" / "skills"))
    assert cs.targets("mac") == [("mac", str(tmp_path / ".claude" / "skills"), True),
                                 ("mac:work", str(tmp_path / ".claude-work" / "skills"), False)]


def test_other_account_gets_its_own_copy_and_old_sync_links_dont_count(tmp_path, monkeypatch):
    claude, work = tmp_path / "claude", tmp_path / "claude-work" / "skills"
    monkeypatch.setattr(cs, "SKILLS_DIR", str(claude))
    monkeypatch.setattr(cs, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(cs, "BACKUP_DIR", str(tmp_path / "bk"))
    cs.write_skill("handoff", {"SKILL.md": "h"})                       # conta padrão
    work.mkdir(parents=True)
    os.symlink(claude / "handoff", work / "handoff")                    # link do sync anterior
    (work / "worklog").mkdir()
    (work / "worklog" / "SKILL.md").write_text("só na conta de trabalho")
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "SKILL.md").write_text("de um repo git")
    os.symlink(tmp_path / "repo", work / "knowledge")                   # link próprio do usuário
    found = cs.local_skills(str(work))
    assert set(found) == {"worklog", "knowledge"}                       # link do sync não conta
    assert cs.write_skill("handoff", {"SKILL.md": "h"}, base=str(work)) == "instalada"
    assert not os.path.islink(work / "handoff") and (work / "handoff" / "SKILL.md").read_text() == "h"
    assert cs.write_skill("knowledge", {"SKILL.md": "x"}, base=str(work)).startswith("link")
    assert not os.path.exists(tmp_path / "agents" / "worklog")          # Codex só pela conta padrão


def test_extra_claude_dirs_autodetects_config_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_EXTRA_SKILL_DIRS")
    monkeypatch.setattr(cs, "ENV", {})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cs, "SKILLS_DIR", str(tmp_path / ".claude" / "skills"))
    (tmp_path / ".claude-work").mkdir()
    (tmp_path / ".claude-work" / "settings.json").write_text("{}")
    (tmp_path / ".claude-cache").mkdir()                                       # sem settings.json
    assert cs.extra_claude_dirs() == [str(tmp_path / ".claude-work" / "skills")]


def test_write_skill_refuses_unsafe_and_respects_links(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SKILLS_DIR", str(tmp_path / "claude"))
    with pytest.raises(ValueError):
        cs.write_skill("../evil", {"SKILL.md": "x"})
    with pytest.raises(ValueError):
        cs.write_skill("ok", {"../../x": "x"})
    (tmp_path / "repo").mkdir()
    (tmp_path / "claude").mkdir()
    os.symlink(tmp_path / "repo", tmp_path / "claude" / "linked")
    assert cs.write_skill("linked", {"SKILL.md": "x"}).startswith("link")


def test_read_skill_requires_skill_md_and_skips_binary(tmp_path):
    (tmp_path / "s").mkdir()
    assert cs.read_skill(str(tmp_path / "s")) is None
    (tmp_path / "s" / "SKILL.md").write_text("# s")
    (tmp_path / "s" / "img.png").write_bytes(b"\x89PNG\0\0data")
    assert cs.read_skill(str(tmp_path / "s")) == {"SKILL.md": "# s"}


# ---- disparo na abertura de sessão --------------------------------------------------------

def test_should_sync_respects_interval(tmp_path):
    import recall_session as rs
    stamp = tmp_path / "stamp"
    assert rs.should_sync(str(stamp), now=1000)                    # nunca rodou
    stamp.write_text("")
    t = os.path.getmtime(stamp)
    assert not rs.should_sync(str(stamp), now=t + 60)
    assert rs.should_sync(str(stamp), now=t + rs.SYNC_MIN_INTERVAL + 1)


def test_start_background_sync_detaches_and_rate_limits(tmp_path, monkeypatch):
    import subprocess
    import recall_session as rs
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(rs, "SYNC_STAMP", str(tmp_path / "stamp"))
    monkeypatch.setattr(rs, "HERE", str(tmp_path))
    monkeypatch.delenv("SESSION_SYNC", raising=False)
    assert rs.start_background_sync({}) is True
    assert calls[0][1]["start_new_session"] is True                # sobrevive ao fim do hook
    assert calls[0][0][0][-1].endswith("config_sync.py")
    assert rs.start_background_sync({}) is False                   # dentro do intervalo
    (tmp_path / "stamp").unlink()
    assert rs.start_background_sync({"SESSION_SYNC": "0"}) is False
    assert len(calls) == 1


def test_main_skips_when_another_sync_holds_the_lock(tmp_path, monkeypatch, capsys):
    import fcntl
    monkeypatch.setattr(cs, "URL", "https://x")
    monkeypatch.setattr(cs, "KEY", "k")
    monkeypatch.setattr(cs, "LOCK_PATH", str(tmp_path / "lock"))
    held = open(tmp_path / "lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert cs.main([]) == 0
    assert "já está rodando" in capsys.readouterr().out
