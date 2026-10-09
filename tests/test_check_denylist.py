"""Unit tests for the commit denylist guard — pure parsing/matching, no git."""
import check_denylist as cd


def test_parse_terms_normalises():
    assert cd.parse_terms(" Acme, globex,,ACME ") == ["acme", "globex"]
    assert cd.parse_terms("") == []
    assert cd.parse_terms(None) == []


def test_find_hits_is_case_insensitive_substring():
    items = [("a.py:1", "remote = 'git@github.com:AcmeCorp/app.git'"), ("a.py:2", "clean line")]
    assert cd.find_hits(items, ["acme"]) == [("a.py:1", "acme")]


def test_find_hits_reports_every_term():
    assert cd.find_hits([("m", "acme and globex")], ["acme", "globex"]) == [("m", "acme"), ("m", "globex")]


def test_staged_items_numbers_added_lines_and_includes_paths():
    diff = "\n".join([
        "diff --git a/x.py b/x.py",
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -3,0 +4,2 @@",
        "+first added",
        "+second added",
        "@@ -10 +12 @@",
        "-removed acme line",
        "+replacement",
    ])
    items = cd.staged_items(diff, ["x.py"])
    assert items == [("x.py (path)", "x.py"), ("x.py:4", "first added"),
                     ("x.py:5", "second added"), ("x.py:12", "replacement")]


def test_staged_items_ignores_removed_lines():
    diff = "+++ b/x.py\n@@ -1 +1 @@\n-acme here\n+generic here"
    assert cd.find_hits(cd.staged_items(diff, []), ["acme"]) == []


def test_staged_items_skips_deleted_files():
    diff = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-acme"
    assert cd.staged_items(diff, []) == []
