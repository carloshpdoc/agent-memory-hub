"""
agent-memory-hub — dashboard (optional). Read-mostly web view of the shared Supabase:
operation (nightly, capture per machine, extraction queue), memory (facts with decayed
confidence, sessions with search), profile (approve/reject patterns) and recall.

Same-origin: this app serves the static PWA and the /api it calls. Every /api route
needs `Authorization: Bearer $DASHBOARD_TOKEN`; the Supabase secret key stays here.

Config (env, or the repo .env when run from a clone):
  SUPABASE_URL, SUPABASE_SECRET_KEY, DASHBOARD_TOKEN (required)

Run locally:
  DASHBOARD_TOKEN=dev uv run --with-requirements dashboard/requirements.txt \
    uvicorn --app-dir dashboard app:app --port 8095
"""
import hmac
import os
import re
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# no servidor o deploy copia memory_client/recall_session/project_key para lib/;
# num clone, usa os originais do repo (sem duplicar codigo)
for p in (os.path.join(HERE, "lib"), os.path.join(HERE, "..", "scripts"),
          os.path.join(HERE, "..", "hooks")):
    if os.path.isdir(p):
        sys.path.insert(0, p)

from fastapi import Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import memory_client as mc  # noqa: E402
from recall_session import decayed_conf  # noqa: E402

TOKEN = os.environ.get("DASHBOARD_TOKEN") or mc.ENV.get("DASHBOARD_TOKEN")
if not TOKEN or not mc.URL or not mc.KEY:
    raise SystemExit("DASHBOARD_TOKEN, SUPABASE_URL e SUPABASE_SECRET_KEY sao obrigatorios")

STATIC = os.path.join(HERE, "static")
PATTERN_STATUSES = ("proposed", "approved", "rejected")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z._-]{1,80}$")
DAILY_DAYS = 30
SESSION_CONTENT_CHARS = 20_000

app = FastAPI(title="agent-memory-hub dashboard", docs_url=None, redoc_url=None, openapi_url=None)


def auth(request: Request):
    header = request.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    if not given or not hmac.compare_digest(given, TOKEN):
        raise HTTPException(401, "token invalido")


def q(value):
    return urllib.parse.quote(str(value), safe="")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/overview", dependencies=[Depends(auth)])
def overview():
    now = datetime.now(timezone.utc)
    sessions = mc.rest_all("sessions?select=machine,tool,started_at,facts_extracted_at")
    groups = defaultdict(lambda: {"sessions": 0, "pending": 0, "last": None})
    since = (now - timedelta(days=DAILY_DAYS - 1)).date()
    days = [(since + timedelta(days=i)).isoformat() for i in range(DAILY_DAYS)]
    daily = defaultdict(lambda: dict.fromkeys(days, 0))
    for s in sessions:
        g = groups[(s.get("machine") or "?", s.get("tool") or "?")]
        g["sessions"] += 1
        g["pending"] += s.get("facts_extracted_at") is None
        started = s.get("started_at") or ""
        g["last"] = max(g["last"] or "", started) or None
        if days[0] <= started[:10] <= days[-1]:
            daily[s.get("machine") or "?"][started[:10]] += 1

    machines = []
    for row in mc.rest("ops_status?select=machine,updated_at,payload&order=machine"):
        p = row.get("payload") or {}
        machines.append({"machine": row["machine"], "updated_at": row.get("updated_at"),
                         "nightly": p.get("nightly"), "capture": p.get("capture"),
                         "skills": p.get("skills") or []})
    return {
        "generated_at": now.isoformat(),
        "totals": {
            "sessions": len(sessions),
            "pending": sum(g["pending"] for g in groups.values()),
            "facts": mc.count("facts?select=id&valid_until=is.null"),
            "patterns_proposed": mc.count("profile_patterns?select=id&status=eq.proposed"),
        },
        "groups": [{"machine": m, "tool": t, **g} for (m, t), g in
                   sorted(groups.items(), key=lambda kv: -kv[1]["sessions"])],
        "daily": {"days": days, "by_machine": daily},
        "machines": machines,
    }


@app.get("/api/facts", dependencies=[Depends(auth)])
def facts():
    rows = mc.rest_all("facts?select=id,fact,kind,scope,confidence,valid_from,source_session_id"
                       "&valid_until=is.null&order=valid_from.desc")
    for f in rows:
        eff = decayed_conf(f.get("confidence"), f.get("kind", "fact"), f.get("valid_from"))
        f["effective"] = round(eff, 3) if eff is not None else None
    return rows


@app.get("/api/sessions", dependencies=[Depends(auth)])
def sessions(q_: str = Query("", alias="q"), project: str = "", machine: str = "", limit: int = 50):
    limit = max(1, min(limit, 200))
    path = (f"sessions?select=session_id,project,machine,tool,started_at,summary,facts_extracted_at"
            f"&order=started_at.desc&limit={limit}")
    if q_.strip():
        path += f"&content_tsv=fts(simple).{q(q_.strip())}"
    if project:
        path += f"&project=eq.{q(project)}"
    if machine:
        path += f"&machine=eq.{q(machine)}"
    return mc.rest(path)


@app.get("/api/sessions/{session_id}", dependencies=[Depends(auth)])
def session(session_id: str):
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "session_id invalido")
    rows = mc.rest(f"sessions?select=session_id,project,machine,tool,started_at,ended_at,"
                   f"summary,content,facts_extracted_at&session_id=eq.{q(session_id)}")
    if not rows:
        raise HTTPException(404, "sessao nao encontrada")
    s = rows[0]
    content = s.get("content") or ""
    s["content_chars"] = len(content)
    s["content"] = content[:SESSION_CONTENT_CHARS]
    s["facts"] = mc.rest(f"facts?select=id,fact,kind,confidence,valid_until"
                         f"&source_session_id=eq.{q(session_id)}")
    return s


@app.get("/api/profile", dependencies=[Depends(auth)])
def profile():
    return mc.rest("profile_patterns?select=id,pattern,category,evidence,confidence,status,"
                   "proposed_rule,created_at,reviewed_at&order=status,confidence.desc")


@app.post("/api/profile/{pattern_id}", dependencies=[Depends(auth)])
async def review_pattern(pattern_id: str, request: Request):
    if not UUID_RE.match(pattern_id):
        raise HTTPException(400, "id invalido")
    body = await request.json()
    status = body.get("status") if isinstance(body, dict) else None
    if status not in PATTERN_STATUSES:
        raise HTTPException(400, f"status deve ser um de {PATTERN_STATUSES}")
    reviewed = None if status == "proposed" else datetime.now(timezone.utc).isoformat()
    mc.write(f"profile_patterns?id=eq.{pattern_id}", {"status": status, "reviewed_at": reviewed})
    return {"id": pattern_id, "status": status}


SYNC_STATUSES = ("pending", "approved", "rejected")
MACHINE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@app.get("/api/sync", dependencies=[Depends(auth)])
def sync_matrix():
    """Itens sincronizáveis (fase 1: skills) x máquinas, com o estado de cada par."""
    items = mc.rest_all("config_items?select=id,kind,name,hash,source_machine,updated_at,files"
                        "&order=kind,name")
    assigns = mc.rest_all("config_assignments?select=item_id,machine,status,installed_hash,decided_at")
    machines = sorted({r["machine"] for r in mc.rest("ops_status?select=machine")}
                      | {a["machine"] for a in assigns})
    hashes = {i["id"]: i["hash"] for i in items}
    by_item = defaultdict(dict)
    for a in assigns:
        by_item[a["item_id"]][a["machine"]] = {
            "status": a["status"], "decided_at": a.get("decided_at"),
            "installed": a.get("installed_hash") == hashes.get(a["item_id"])}
    return {"machines": machines, "items": [{
        "id": i["id"], "kind": i["kind"], "name": i["name"], "source_machine": i["source_machine"],
        "updated_at": i["updated_at"], "files": sorted(i["files"]),
        "size": sum(len(v) for v in i["files"].values()),
        "assignments": by_item.get(i["id"], {})} for i in items]}


@app.get("/api/sync/items/{item_id}", dependencies=[Depends(auth)])
def sync_item(item_id: str):
    if not UUID_RE.match(item_id):
        raise HTTPException(400, "id invalido")
    rows = mc.rest(f"config_items?select=name,kind,files,source_machine,updated_at&id=eq.{item_id}")
    if not rows:
        raise HTTPException(404, "item nao encontrado")
    return rows[0]


@app.post("/api/sync/{item_id}", dependencies=[Depends(auth)])
async def decide_sync(item_id: str, request: Request):
    """Aprova/rejeita um item para UMA máquina. A máquina instala no próximo sync."""
    body = await request.json()
    machine = body.get("machine") if isinstance(body, dict) else None
    status = body.get("status") if isinstance(body, dict) else None
    if not UUID_RE.match(item_id) or not machine or not MACHINE_RE.match(machine):
        raise HTTPException(400, "item ou maquina invalido")
    if status not in SYNC_STATUSES:
        raise HTTPException(400, f"status deve ser um de {SYNC_STATUSES}")
    decided = None if status == "pending" else datetime.now(timezone.utc).isoformat()
    mc.write("config_assignments?on_conflict=item_id,machine",
             {"item_id": item_id, "machine": machine, "status": status, "decided_at": decided},
             method="POST", prefer="resolution=merge-duplicates,return=minimal")
    return {"item_id": item_id, "machine": machine, "status": status}


@app.get("/api/recall", dependencies=[Depends(auth)])
def recall():
    events = []
    for row in mc.rest("ops_status?select=machine,payload"):
        for e in (row.get("payload") or {}).get("recall") or []:
            events.append({**e, "machine": row["machine"]})
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return events


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC), name="static")
