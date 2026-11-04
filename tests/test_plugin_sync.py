"""Unit tests for plugin_sync and config_sync.sync_plugin_target — publish, approve per target,
install/uninstall through the CLI (faked), never touching the real Claude config."""
import config_sync as cs
import plugin_sync as ps
import pytest
from test_config_sync import _ConfigStore

GH = {"source": {"source": "github", "repo": "owner/market"}}


def test_to_item_is_portable_and_builtin_marketplace_has_no_source(monkeypatch):
    monkeypatch.setattr(ps.ss, "HOME", "/Users/me")
    name, files = ps.to_item("p@local", {"local": {"source": {"source": "directory", "path": "/Users/me/mk"}}})
    assert name == "claude:p@local" and files["source"] == {"source": "directory", "path": "__HOME__/mk"}
    assert ps.to_item("x@anthropic-plugin-directory", {})[1]["source"] == {}


def test_resolve_adds_unknown_marketplace_and_reports_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(ps.ss, "HOME", str(tmp_path))
    files = ps.to_item("p@market", {"market": GH})[1]
    assert ps.resolve_for_target(files, {}) == ("owner/market", None)
    assert ps.resolve_for_target(files, {"market": GH}) == (None, None)
    assert ps.resolve_for_target(ps.to_item("x@anthropic-plugin-directory", {})[1], {}) == (None, None)
    gone = {"tool": "claude", "id": "p@d", "marketplace": "d", "source": {"source": "directory", "path": "__HOME__/nope"}}
    assert ps.resolve_for_target(gone, {})[1] == "falta dependência: ~/nope"
    assert "inválido" in ps.resolve_for_target({**files, "id": "--evil@x"}, {})[1]


def test_env_drops_inherited_config_dir_for_the_default_account(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/me/.claude-work")
    assert "CLAUDE_CONFIG_DIR" not in ps._env(None)
    assert ps._env("/Users/me/.claude-x")["CLAUDE_CONFIG_DIR"] == "/Users/me/.claude-x"


class _FakeCli:
    """Estado de plugins por conta, no lugar do `claude plugin`."""

    def __init__(self):
        self.installed, self.calls = {}, []

    def patch(self, monkeypatch):
        monkeypatch.setattr(ps, "list_installed", lambda d: {p: {} for p in self.installed.get(d, set())})
        monkeypatch.setattr(ps, "known_marketplaces", lambda d: {"market": GH} if d == "/a" else {})
        monkeypatch.setattr(ps, "install", lambda d, f, arg: (self.calls.append(("install", d, f["id"], arg)),
                                                              self.installed.setdefault(d, set()).add(f["id"])))
        monkeypatch.setattr(ps, "uninstall", lambda d, pid: (self.calls.append(("uninstall", d, pid)),
                                                             self.installed[d].discard(pid)))


@pytest.fixture
def world(monkeypatch):
    store, cli = _ConfigStore(), _FakeCli()
    monkeypatch.setattr(cs, "_store", lambda: store)
    cli.patch(monkeypatch)
    return store, cli


def _sync(target, d):
    notes = []
    cs.sync_plugin_target(target, d, d, False, lambda a, n: notes.append((a, n)))
    return notes


def test_plugin_installs_only_on_the_target_that_approved_it(world):
    store, cli = world
    cli.installed["/a"] = {"p@market"}
    _sync("A", "/a")
    (item_id,) = store.items
    assert store.asg[(item_id, "A")]["status"] == "approved"
    _sync("B", "/b"), _sync("B:work", "/bw")
    assert store.asg[(item_id, "B")]["status"] == store.asg[(item_id, "B:work")]["status"] == "pending"
    assert cli.calls == []                                  # pendente: nada instalado
    store.set_assignment(item_id, "B", status="approved")
    _sync("B", "/b"), _sync("B:work", "/bw")
    assert cli.calls == [("install", "/b", "p@market", "owner/market")]   # só a conta aprovada
    assert store.asg[(item_id, "B")]["installed_hash"] == store.items[item_id]["hash"]
    assert "p@market" not in cli.installed.get("/bw", set())


def test_already_installed_counts_as_approval_and_reject_uninstalls(world):
    store, cli = world
    cli.installed = {"/a": {"p@market"}, "/b": {"p@market"}}
    _sync("A", "/a"), _sync("B", "/b")
    (item_id,) = store.items
    assert store.asg[(item_id, "B")]["status"] == "approved" and cli.calls == []
    store.set_assignment(item_id, "B", status="rejected")
    _sync("B", "/b")
    assert cli.calls == [("uninstall", "/b", "p@market")]
    assert store.asg[(item_id, "B")]["installed_hash"] is None


def test_install_failure_is_recorded_not_swallowed(world, monkeypatch):
    store, cli = world
    cli.installed["/a"] = {"p@market"}
    _sync("A", "/a")
    (item_id,) = store.items
    store.set_assignment(item_id, "B", status="approved")

    def boom(*a):
        raise RuntimeError("needs -y to accept command")
    monkeypatch.setattr(ps, "install", boom)
    notes = _sync("B", "/b")
    assert ("plugin falhou", "p@market: needs -y to accept command") in notes
    assert store.asg[(item_id, "B")]["note"] == "falhou ao instalar: needs -y to accept command"
    assert not store.asg[(item_id, "B")].get("installed_hash")


def test_removed_at_origin_retires_and_new_targets_get_no_pending(world):
    store, cli = world
    cli.installed["/a"] = {"p@market"}
    _sync("A", "/a")
    cli.installed["/a"] = set()
    _sync("A", "/a")
    (item_id,) = store.items
    assert store.items[item_id]["retired_at"]
    _sync("C", "/c")
    assert (item_id, "C") not in store.asg


def test_unreadable_plugin_list_changes_nothing(world, monkeypatch):
    store, _ = world
    monkeypatch.setattr(ps, "list_installed", lambda d: (_ for _ in ()).throw(RuntimeError("no cli")))
    notes = _sync("A", "/a")
    assert notes[0][0] == "lista de plugins ilegível; não mexi" and store.items == {} and store.asg == {}
