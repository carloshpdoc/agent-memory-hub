#!/usr/bin/env python3
"""
agent-memory-hub — per-machine operational snapshot for the dashboard.

Condenses this machine's local state (capture.log, recall.log, nightly-status.json and
the installed Claude Code skills) into one `ops_status` row keyed by hostname. Called
at the end of the capture hook, throttled to one push every OPS_PUSH_MINUTES (30), and
by the nightly job with force=True. Never raises: a dashboard must not break capture.

Usage:
  python3 hooks/ops_snapshot.py           # push now (ignores the throttle)
  python3 hooks/ops_snapshot.py --print   # show the payload, push nothing
"""
import glob
import json
import os
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CAPTURE_LOG = os.path.join(HERE, "capture.log")
RECALL_LOG = os.path.join(HERE, "recall.log")
NIGHTLY_STATUS = os.path.join(REPO, "nightly-status.json")
STAMP_PATH = os.path.join(HERE, ".ops_pushed")
SKILL_DIRS = ("~/.claude/skills", "~/.claude-work/skills")
RECALL_KEEP = 200       # ultimas injecoes enviadas (so contagens, sem o texto dos fatos)
TAIL_BYTES = 2_000_000  # os logs crescem sem limite; so o fim interessa


def _tail_lines(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines  # a primeira pode vir cortada


def capture_stats(now):
    cutoff = (now - timedelta(hours=24)).isoformat()
    ok = err = 0
    last_ok = last_err = None
    for ln in _tail_lines(CAPTURE_LOG):
        ts = ln[:32]
        is_ok = "OK sessao" in ln
        is_err = "stdin invalido" in ln or "HTTPError" in ln or "erro ao salvar" in ln
        if is_ok:
            last_ok = ts
        if is_err:
            last_err = {"ts": ts, "line": ln[33:233]}
        if ts >= cutoff:
            ok += is_ok
            err += is_err
    return {"ok_24h": ok, "err_24h": err, "last_ok": last_ok, "last_err": last_err}


def recall_events():
    out = []
    for ln in _tail_lines(RECALL_LOG)[-RECALL_KEEP:]:
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        out.append({"ts": d.get("ts"), "project": d.get("project"), "source": d.get("source"),
                    "style": d.get("style"), "est_tokens": d.get("est_tokens"),
                    "facts": len(d.get("facts") or []), "sessions": len(d.get("sessions") or []),
                    "dropped_facts": d.get("dropped_facts", 0),
                    "dropped_sessions": d.get("dropped_sessions", 0)})
    return out


def _skill_description(skill_md):
    try:
        with open(skill_md) as f:
            text = f.read(4000)
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    for ln in text.split("---", 2)[1].splitlines():
        if ln.startswith("description:"):
            return ln.split(":", 1)[1].strip().strip('"\'')[:300]
    return None


def skills():
    out = []
    for base in SKILL_DIRS:
        root = os.path.expanduser(base)
        for skill_md in sorted(glob.glob(os.path.join(root, "*", "SKILL.md"))):
            out.append({"name": os.path.basename(os.path.dirname(skill_md)), "dir": base,
                        "description": _skill_description(skill_md),
                        "modified": datetime.fromtimestamp(os.path.getmtime(skill_md),
                                                           timezone.utc).isoformat()})
    return out


def nightly():
    try:
        with open(NIGHTLY_STATUS) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def build_payload():
    now = datetime.now(timezone.utc)
    return {"generated_at": now.isoformat(), "nightly": nightly(),
            "capture": capture_stats(now), "recall": recall_events(), "skills": skills()}


def _throttled(minutes):
    try:
        return time.time() - os.path.getmtime(STAMP_PATH) < minutes * 60
    except OSError:
        return False


def push(env, force=False):
    """Upsert the snapshot. Returns True when pushed; never raises."""
    try:
        url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
        if not url or not key:
            return False
        if not force and _throttled(int(env.get("OPS_PUSH_MINUTES", "30"))):
            return False
        row = {"machine": socket.gethostname(), "updated_at": datetime.now(timezone.utc).isoformat(),
               "payload": build_payload()}
        req = urllib.request.Request(
            f"{url}/rest/v1/ops_status?on_conflict=machine",
            data=json.dumps(row).encode("utf-8"), method="POST",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        with open(STAMP_PATH, "w") as f:
            f.write(row["updated_at"])
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    from capture_session import load_env, ENV_PATH
    if "--print" in sys.argv:
        print(json.dumps(build_payload(), indent=2, ensure_ascii=False)[:4000])
        sys.exit(0)
    sys.exit(0 if push({**load_env(ENV_PATH), **os.environ}, force=True) else 1)
