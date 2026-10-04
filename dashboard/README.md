# dashboard (optional)

Web view of the shared Supabase, installable as a PWA (iPhone: Share → Add to Home Screen;
Mac: Safari → File → Add to Dock).

| Tab | What it shows | Source |
|---|---|---|
| Operação | totals, nightly steps per machine, capture health, sessions/day, machine × tool table, installed skills | `sessions`, `facts`, `profile_patterns`, `ops_status` |
| Memória | valid facts with confidence decayed by age (same formula as recall); sessions with full-text search and transcript | `facts`, `sessions` |
| Perfil | proposed / approved / rejected patterns, approve-reject-reopen buttons (= `mem profile approve`) | `profile_patterns` |
| Recall | tokens per injection, cuts, index vs full mode | `ops_status` (from each machine's `recall.log`) |

Local files (capture.log, recall.log, nightly-status.json, `~/.claude/skills`) reach the
dashboard through `ops_status`: the capture hook upserts one row per machine at most every
`OPS_PUSH_MINUTES` (30), the nightly job right after it runs (`hooks/ops_snapshot.py`).
Needs `sql/08-ops-status.sql` (`scripts/migrate.py`).

## Security

Same origin: the FastAPI app serves the static page and the `/api` it calls. Every `/api`
route requires `Authorization: Bearer $DASHBOARD_TOKEN` (generated on first provision);
the Supabase secret key lives only in the server's `.env`. The app listens on 127.0.0.1 and
is exposed through the Cloudflare Tunnel.

## Run locally

```bash
DASHBOARD_TOKEN=dev uv run --with-requirements dashboard/requirements.txt \
  uvicorn --app-dir dashboard app:app --port 8095
# open http://localhost:8095 and paste "dev"
```

## Deploy (EC2 + Cloudflare Tunnel, SSH over SSM)

```bash
cp dashboard/deploy.env.example dashboard/deploy.env   # your instance, key, tunnel, hostname (gitignored)
bash dashboard/deploy.sh            # bundle + rsync over SSM + provision (127.0.0.1:8095)
bash dashboard/deploy.sh --expose   # + ingress rule and DNS for DASHBOARD_HOST
```

The first provision prints the `DASHBOARD_TOKEN`; it stays in `/opt/amh-dashboard/.env`.
Redeploys keep it.
