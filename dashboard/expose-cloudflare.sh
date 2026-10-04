#!/usr/bin/env bash
# Expoe o dashboard via Cloudflare Tunnel. Roda NO SERVIDOR (ec2-user + sudo).
# Adiciona uma regra de ingress ($HOST -> localhost:8095) antes do catch-all
# e cria o registro DNS. Idempotente.
set -euo pipefail

# TUNNEL_ID e DASHBOARD_HOST chegam do deploy.sh (que le dashboard/deploy.env)
TUNNEL_ID="${TUNNEL_ID:?TUNNEL_ID obrigatorio}"
HOST="${DASHBOARD_HOST:?DASHBOARD_HOST obrigatorio}"
PORT=8095
CFG=/etc/cloudflared/config.yml

echo "==> [1/4] backup do config"
sudo cp "$CFG" "${CFG}.bak.$(date +%s)"

echo "==> [2/4] insere ingress (se ainda nao existe)"
if sudo grep -q "$HOST" "$CFG"; then
  echo "    ja presente, pulando"
else
  sudo python3 - "$CFG" "$HOST" "$PORT" <<'PY'
import sys
cfg, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(cfg).read().splitlines()
out, inserted = [], False
for ln in lines:
    # insere antes do catch-all http_status:404
    if not inserted and "http_status:404" in ln:
        indent = ln[:len(ln) - len(ln.lstrip())]
        out.append(f"{indent}- hostname: {host}")
        out.append(f"{indent}  service: http://localhost:{port}")
        inserted = True
    out.append(ln)
if not inserted:
    raise SystemExit("catch-all http_status:404 nao encontrado — nao mexi no config")
open(cfg, "w").write("\n".join(out) + "\n")
print("    regra inserida")
PY
fi

echo "==> [3/4] DNS route (cria CNAME $HOST -> tunnel)"
sudo cloudflared tunnel route dns "$TUNNEL_ID" "$HOST" 2>&1 | sed 's/^/    /' || echo "    (registro provavelmente ja existe — ok)"

echo "==> [4/4] restart cloudflared"
sudo systemctl restart cloudflared
sleep 2
sudo systemctl --no-pager --lines=0 status cloudflared | head -4 || true
echo "==> OK — teste: https://$HOST/healthz"
