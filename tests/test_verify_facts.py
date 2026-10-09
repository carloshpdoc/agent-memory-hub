"""Unit tests for verify_facts — reference extraction and the existence check on a temp repo."""
import subprocess

import recall_session as rs
import verify_facts as vf


def test_file_refs_keeps_paths_with_code_extension():
    text = ("Edite `Filters/Source/FilterSelection.swift` e scripts/run.sh; "
            "veja app/articles/[slug]/page.tsx")
    assert vf.file_refs(text) == ["Filters/Source/FilterSelection.swift", "scripts/run.sh",
                                  "app/articles/[slug]/page.tsx"]


def test_file_refs_ignores_noise():
    text = ("branch feature/ACME-12, repo carloshpdoc/agent-memory-hub, pasta dist/, "
            "arquivo solto CLAUDE.md, relativo ./x/y.py, url https://x.com/a/b.html")
    assert vf.file_refs(text) == []


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "app" / "src").mkdir(parents=True)
    (tmp_path / "app" / "src" / "main.py").write_text("x")
    (tmp_path / ".gitignore").write_text("build/\n")
    git("add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return str(tmp_path)


def test_missing_refs_accepts_tracked_suffix_disk_and_ignored(tmp_path):
    root = _repo(tmp_path)
    tracked = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True).stdout.splitlines()
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "todo.md").write_text("untracked but on disk")
    refs = ["app/src/main.py", "src/main.py", "notes/todo.md", "build/out.js", "src/gone.py"]
    assert vf.missing_refs(root, refs, tracked) == ["src/gone.py"]


def test_changed_only_when_result_differs():
    new = vf.check_value("abc", ["a/b.py"], [], "now")
    assert not vf.changed({"refs": 1, "missing": [], "checked_at": "old"}, new)
    assert vf.changed({"refs": 1, "missing": ["a/b.py"]}, new)
    assert vf.changed(None, new)


def test_recall_flags_and_reads_stale_refs():
    assert rs.stale_refs({"code_check": {"missing": ["a/b.py"]}}) == ["a/b.py"]
    assert rs.stale_refs({"code_check": None}) == [] and rs.stale_refs({}) == []
    text, _ = rs.assemble_context("p", [(0.4, {"fact": "uses a/b.py", "kind": "fact", "scope": "p",
                                               "confidence": 0.8, "code_check": {"missing": ["a/b.py"]}})],
                                  [], [], 4000)
    assert "⚠ cita arquivo que não existe mais: `a/b.py`" in text
