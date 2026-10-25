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
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# no servidor o deploy copia store/recall_session/project_key/handoff para lib/;
# num clone, usa os originais do repo (sem duplicar codigo)
for p in (os.path.join(HERE, "lib"), os.path.join(HERE, "..", "scripts"),
          os.path.join(HERE, "..", "hooks")):
    if os.path.isdir(p):
        sys.path.insert(0, p)

from fastapi import Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from recall_session import decayed_conf  # noqa: E402
from store import config, get_store  # noqa: E402

S = get_store()
TOKEN = config.get("DASHBOARD_TOKEN")
if not TOKEN or not S.configured:
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/overview", dependencies=[Depends(auth)])
def overview():
    now = datetime.now(timezone.utc)
    sessions = S.session_activity()
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
    for row in S.ops_statuses():
        p = row.get("payload") or {}
        machines.append({"machine": row["machine"], "updated_at": row.get("updated_at"),
                         "nightly": p.get("nightly"), "capture": p.get("capture"),
                         "skills": p.get("skills") or []})
    return {
        "generated_at": now.isoformat(),
        "totals": {
            "sessions": len(sessions),
            "pending": sum(g["pending"] for g in groups.values()),
            "facts": S.valid_fact_count(),
            "patterns_proposed": S.pattern_count("proposed"),
        },
        "groups": [{"machine": m, "tool": t, **g} for (m, t), g in
                   sorted(groups.items(), key=lambda kv: -kv[1]["sessions"])],
        "daily": {"days": days, "by_machine": daily},
        "machines": machines,
    }


@app.get("/api/facts", dependencies=[Depends(auth)])
def facts():
    rows = S.valid_fact_details()
    for f in rows:
        eff = decayed_conf(f.get("confidence"), f.get("kind", "fact"), f.get("valid_from"))
        f["effective"] = round(eff, 3) if eff is not None else None
    return rows


@app.get("/api/sessions", dependencies=[Depends(auth)])
def sessions(q_: str = Query("", alias="q"), project: str = "", machine: str = "", limit: int = 50):
    limit = max(1, min(limit, 200))
    return S.list_sessions(q_.strip() or None, project or None, machine or None, limit)


@app.get("/api/sessions/{session_id}", dependencies=[Depends(auth)])
def session(session_id: str):
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "session_id invalido")
    s = S.session_detail(session_id)
    if not s:
        raise HTTPException(404, "sessao nao encontrada")
    content = s.get("content") or ""
    s["content_chars"] = len(content)
    s["content"] = content[:SESSION_CONTENT_CHARS]
    s["facts"] = S.facts_from_session(session_id)
    return s


@app.get("/api/profile", dependencies=[Depends(auth)])
def profile():
    return S.all_patterns()


@app.post("/api/profile/{pattern_id}", dependencies=[Depends(auth)])
async def review_pattern(pattern_id: str, request: Request):
    if not UUID_RE.match(pattern_id):
        raise HTTPException(400, "id invalido")
    body = await request.json()
    status = body.get("status") if isinstance(body, dict) else None
    if status not in PATTERN_STATUSES:
        raise HTTPException(400, f"status deve ser um de {PATTERN_STATUSES}")
    reviewed = None if status == "proposed" else datetime.now(timezone.utc).isoformat()
    S.update_pattern(pattern_id, {"status": status, "reviewed_at": reviewed})
    return {"id": pattern_id, "status": status}


SYNC_STATUSES = ("pending", "approved", "rejected")
MACHINE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}(:[A-Za-z0-9._-]{1,32})?$")   # host[:conta]


@app.get("/api/sync", dependencies=[Depends(auth)])
def sync_matrix():
    """Itens sincronizáveis (fase 1: skills) x máquinas, com o estado de cada par."""
    items = S.config_items()
    assigns = S.all_assignments()
    machines = sorted({r["machine"] for r in S.ops_statuses()}
                      | {a["machine"] for a in assigns})
    hashes = {i["id"]: i["hash"] for i in items}
    by_item = defaultdict(dict)
    for a in assigns:
        by_item[a["item_id"]][a["machine"]] = {
            "status": a["status"], "decided_at": a.get("decided_at"), "note": a.get("note"),
            "installed": a.get("installed_hash") == hashes.get(a["item_id"])}
    return {"machines": machines, "items": [{
        "id": i["id"], "kind": i["kind"], "name": i["name"], "source_machine": i["source_machine"],
        "updated_at": i["updated_at"], "files": sorted(i["files"]),
        "size": sum(len(v) for v in i["files"].values()), "retired_at": i.get("retired_at"),
        # hooks/permissões são curtos: vão inteiros para a tela mostrar sem abrir
        "content": i["files"] if i["kind"] in ("hook", "permission", "mcp") else None,
        "assignments": by_item.get(i["id"], {})} for i in items]}


@app.post("/api/sync/bulk", dependencies=[Depends(auth)])
async def decide_sync_bulk(request: Request):
    """Mesma decisão para vários itens numa máquina (ex.: aprovar todas as permissões)."""
    body = await request.json()
    ids = body.get("item_ids") if isinstance(body, dict) else None
    machine, status = (body or {}).get("machine"), (body or {}).get("status")
    if (not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, str) and UUID_RE.match(i) for i in ids)
            or not machine or not MACHINE_RE.match(machine) or status not in SYNC_STATUSES):
        raise HTTPException(400, "pedido invalido")
    decided = None if status == "pending" else datetime.now(timezone.utc).isoformat()
    S.set_assignments(ids, machine, status=status, decided_at=decided)
    return {"machine": machine, "status": status, "count": len(ids)}


@app.get("/api/sync/items/{item_id}", dependencies=[Depends(auth)])
def sync_item(item_id: str):
    if not UUID_RE.match(item_id):
        raise HTTPException(400, "id invalido")
    item = S.config_item(item_id)
    if not item:
        raise HTTPException(404, "item nao encontrado")
    return {k: item.get(k) for k in ("name", "kind", "files", "source_machine", "updated_at")}


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
    S.set_assignment(item_id, machine, status=status, decided_at=decided)
    return {"item_id": item_id, "machine": machine, "status": status}


@app.get("/api/recall", dependencies=[Depends(auth)])
def recall():
    events = []
    for row in S.ops_statuses():
        for e in (row.get("payload") or {}).get("recall") or []:
            events.append({**e, "machine": row["machine"]})
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return events


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC), name="static")
