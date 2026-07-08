"""
Tests for facts->enforcement (roadmap item 12): the LLM-verdict parser and the
generated guard itself — the guard is written to a temp file and EXECUTED against
tool-call payloads to prove it blocks violations (exit 2) and allows the rest.
"""
import json
import subprocess
import sys

import enforce_rules as er


# ---- parse_enforce: default nao-mecanizavel ------------------------------------------

def test_parse_enforce_valid():
    out = er.parse_enforce(json.dumps(
        {"enforceable": True, "pattern": r"git\s+push\s+.*--force", "message": "sem force push"}))
    assert out == {"pattern": r"git\s+push\s+.*--force", "message": "sem force push"}


def test_parse_enforce_not_enforceable():
    assert er.parse_enforce('{"enforceable": false}') is None


def test_parse_enforce_missing_fields():
    assert er.parse_enforce('{"enforceable": true, "pattern": ""}') is None
    assert er.parse_enforce('{"enforceable": true, "pattern": "x"}') is None  # sem message


def test_parse_enforce_invalid_regex_rejected():
    assert er.parse_enforce(
        '{"enforceable": true, "pattern": "([", "message": "m"}') is None


def test_parse_enforce_garbage():
    assert er.parse_enforce("not json") is None
    assert er.parse_enforce("") is None


# ---- o guard gerado roda e decide certo ----------------------------------------------

RULES = [
    {"pattern": r"git\s+push\s+(\S+\s+)*--force\b", "message": "nunca force push"},
    {"pattern": r"\brm\s+-rf\s+/(\s|$)", "message": "nunca rm -rf /"},
]


def _run_guard(guard_path, payload):
    return subprocess.run([sys.executable, str(guard_path)],
                          input=json.dumps(payload), capture_output=True, text=True)


def _write_guard(tmp_path):
    p = tmp_path / "guard.py"
    p.write_text(er.build_guard(RULES))
    return p


def test_guard_blocks_violation_with_message(tmp_path):
    g = _write_guard(tmp_path)
    r = _run_guard(g, {"tool_input": {"command": "git push origin main --force"}})
    assert r.returncode == 2
    assert "nunca force push" in r.stderr


def test_guard_allows_innocent_command(tmp_path):
    g = _write_guard(tmp_path)
    r = _run_guard(g, {"tool_input": {"command": "git push origin main"}})
    assert r.returncode == 0


def test_guard_allows_non_bash_payload(tmp_path):
    g = _write_guard(tmp_path)
    r = _run_guard(g, {"tool_input": {"file_path": "/tmp/x.py"}})
    assert r.returncode == 0


def test_guard_survives_garbage_stdin(tmp_path):
    g = _write_guard(tmp_path)
    r = subprocess.run([sys.executable, str(g)], input="not json",
                       capture_output=True, text=True)
    assert r.returncode == 0  # guard quebrado nao pode travar a sessao inteira


def test_guard_is_valid_python():
    compile(er.build_guard(RULES), "<guard>", "exec")
