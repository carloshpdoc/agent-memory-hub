"""
Tests for the per-machine ops snapshot: condensing capture.log / recall.log / skills into
the ops_status payload, and the push throttle. No network.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import ops_snapshot as ops


def test_capture_stats_counts_last_24h_only(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=30)).isoformat()
    new = (now - timedelta(hours=1)).isoformat()
    log = tmp_path / "capture.log"
    log.write_text(f"{old} OK sessao a salva\n{old} HTTPError 500 ao salvar a\n"
                   f"{new} OK sessao b salva\n{new} OK sessao c salva\n")
    monkeypatch.setattr(ops, "CAPTURE_LOG", str(log))
    st = ops.capture_stats(now)
    assert (st["ok_24h"], st["err_24h"]) == (2, 0)
    assert st["last_ok"] == new
    assert st["last_err"]["ts"] == old


def test_recall_events_keep_counts_not_fact_text(tmp_path, monkeypatch):
    log = tmp_path / "recall.log"
    log.write_text(json.dumps({"ts": "t1", "project": "p", "est_tokens": 900,
                               "facts": ["secret-ish fact text"], "sessions": ["a", "b"],
                               "dropped_facts": 1}) + "\nnot json\n")
    monkeypatch.setattr(ops, "RECALL_LOG", str(log))
    [e] = ops.recall_events()
    assert e["facts"] == 1 and e["sessions"] == 2 and e["dropped_facts"] == 1
    assert "secret-ish" not in json.dumps(e)


def test_skills_read_frontmatter_description(tmp_path, monkeypatch):
    skill = tmp_path / "skills" / "reembolso"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: reembolso\ndescription: "CFO pessoal"\n---\nbody\n')
    (tmp_path / "skills" / "no-skill-md").mkdir()
    monkeypatch.setattr(ops, "SKILL_DIRS", (str(tmp_path / "skills"),))
    [s] = ops.skills()
    assert s["name"] == "reembolso" and s["description"] == "CFO pessoal"


def test_push_is_throttled_and_never_raises(tmp_path, monkeypatch):
    stamp = tmp_path / ".ops_pushed"
    stamp.write_text("x")
    monkeypatch.setattr(ops, "STAMP_PATH", str(stamp))
    env = {"SUPABASE_URL": "http://127.0.0.1:9", "SUPABASE_SECRET_KEY": "k"}
    assert ops.push(env) is False  # carimbo recente: nem tenta
    os.utime(stamp, (time.time() - 3600, time.time() - 3600))
    assert ops.push(env) is False  # tenta, falha a conexao, engole o erro
    assert ops.push({}) is False
