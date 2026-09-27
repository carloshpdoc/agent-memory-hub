"""Canonical project key: clones share a key, generic dirs have none, facts never go global."""
import os
import subprocess

import pytest

import project_key as pk


def _git_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


@pytest.mark.parametrize("url,name", [
    ("git@github.com:acme/travel-app.git", "travel-app"),
    ("https://github.com/carloshpdoc/knowledge", "knowledge"),
    ("https://github.com/carloshpdoc/knowledge.git/", "knowledge"),
    ("ssh://git@host:22/team/Repo.git", "Repo"),
])
def test_repo_name_from_remote(url, name):
    assert pk.repo_name_from_remote(url) == name


def test_clones_of_the_same_repo_share_one_key(tmp_path):
    env = {"WORKSPACE_ROOTS": str(tmp_path)}
    remote = "git@github.com:acme/travel-app.git"
    for name in ("travel-app", "TravelAppiOS", "travel-app-new"):
        repo = _git_repo(tmp_path / name, remote)
        assert pk.project_key(str(repo), env) == "travel-app"
        (repo / "Sub" / "Dir").mkdir(parents=True)
        assert pk.project_key(str(repo / "Sub" / "Dir"), env) == "travel-app"


def test_repo_without_remote_uses_toplevel_name(tmp_path):
    repo = _git_repo(tmp_path / "MyTool")
    (repo / "src").mkdir()
    assert pk.project_key(str(repo / "src"), {"WORKSPACE_ROOTS": str(tmp_path)}) == "mytool"


def test_non_git_dir_under_workspace_uses_first_segment(tmp_path):
    (tmp_path / "financial" / "Empresa").mkdir(parents=True)
    env = {"WORKSPACE_ROOTS": str(tmp_path)}
    assert pk.project_key(str(tmp_path / "financial" / "Empresa"), env) == "financial"


@pytest.mark.parametrize("cwd", ["/", "", None])
def test_generic_dirs_have_no_project(cwd, tmp_path):
    assert pk.project_key(cwd, {"WORKSPACE_ROOTS": str(tmp_path)}) is None


def test_workspace_root_itself_and_dirs_outside_it_have_no_project(tmp_path):
    ws = tmp_path / "Development"
    ws.mkdir()
    (tmp_path / "Downloads").mkdir()
    env = {"WORKSPACE_ROOTS": str(ws)}
    assert pk.project_key(str(ws), env) is None
    assert pk.project_key(str(tmp_path / "Downloads"), env) is None


def test_repo_at_home_or_workspace_root_is_ignored(tmp_path):
    ws = _git_repo(tmp_path / "Development", "git@github.com:me/dotfiles.git")
    (ws / "notes").mkdir()
    env = {"WORKSPACE_ROOTS": str(ws)}
    assert pk.project_key(str(ws), env) is None
    assert pk.project_key(str(ws / "notes"), env) == "notes"


def test_path_from_another_machine_maps_to_local_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _git_repo(tmp_path / "Development" / "shop-app-clone", "git@github.com:acme/shop-app.git")
    env = {"WORKSPACE_ROOTS": str(tmp_path / "Development")}
    other = "/Users/someone-else/Development/shop-app-clone/Modules"
    assert pk.project_key(other, env) == "shop-app"
    # not cloned here: falls back to the first segment under the workspace root
    assert pk.project_key("/Users/someone-else/Development/portal-webapp/src", env) == "portal-webapp"


def test_aliases_apply_after_resolution(tmp_path):
    env = {"WORKSPACE_ROOTS": str(tmp_path), "PROJECT_ALIASES": "shop-app-clone*=shop-app"}
    (tmp_path / "shop-app-clone3").mkdir()
    assert pk.project_key(str(tmp_path / "shop-app-clone3"), env) == "shop-app"


@pytest.mark.parametrize("llm,session,expected", [
    ("swiftlang/swift", None, "swift"),          # LLM refines a session opened in /
    ("Travel App", "travel-app", "travel-app"),      # spaces + case normalised onto a known key
    ("Acme Store", "shop-app", "shop-app"),  # unknown LLM scope -> session project
    (None, None, pk.NO_PROJECT_SCOPE),            # never global
    ("unknown-thing", None, pk.NO_PROJECT_SCOPE),
    (123, "x", "x"),                              # garbage from the LLM
])
def test_fact_scope(llm, session, expected):
    known = {"swift", "travel-app", "shop-app", "x"}
    assert pk.fact_scope(llm, session, known) == expected
