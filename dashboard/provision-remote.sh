#!/usr/bin/env bash
# Provisiona o dashboard no EC2. Roda NO SERVIDOR (via ec2-user, usa sudo).
# Idempotente: em redeploy preserva o .env (e o DASHBOARD_TOKEN).
# Fonte esperada em: /home/ec2-user/amh-dashboard-src  (bundle montado pelo deploy.sh)
set -euo pipefail

APP_DIR=/opt/amh-dashboard
SRC_DIR=/home/ec2-user/amh-dashboard-src
SVC_USER=amhdash
SVC_NAME=amh-dashboard
PORT=8095

echo "==> [1/8] porta $PORT livre (ou ja e nossa)"
if sudo ss -ltnp "sport = :$PORT" | grep -q LISTEN && ! systemctl is-active --quiet "$SVC_NAME"; then
  echo "ERRO: 127.0.0.1:$PORT ja esta em uso por outro processo:" >&2
  sudo ss -ltnp "sport = :$PORT" >&2
  exit 1
fi

echo "==> [2/8] usuario de servico ($SVC_USER)"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER" \
    || sudo useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$SVC_USER"
  echo "    criado"
else
  echo "    ja existe"
fi

echo "==> [3/8] codigo (app.py, static/, lib/, requirements) — preserva .env"
sudo mkdir -p "$APP_DIR"
sudo rm -rf "$APP_DIR/static" "$APP_DIR/lib"
sudo cp -r "$SRC_DIR/app.py" "$SRC_DIR/requirements.txt" "$SRC_DIR/static" "$SRC_DIR/lib" "$APP_DIR/"

echo "==> [4/8] .env (gera token so na 1a vez)"
BOOT="$SRC_DIR/.env.bootstrap"
if [ ! -f "$APP_DIR/.env" ]; then
  [ -f "$BOOT" ] || { echo "ERRO: sem $APP_DIR/.env e sem $BOOT" >&2; exit 1; }
  TOKEN="$(openssl rand -hex 32)"
  { cat "$BOOT"; echo "DASHBOARD_TOKEN=$TOKEN"; } | sudo tee "$APP_DIR/.env" >/dev/null
  echo "    .env criado"
  echo "    >>> DASHBOARD_TOKEN (guarde; é o login do dashboard): $TOKEN"
else
  echo "    .env ja existe (token preservado)"
fi
rm -f "$BOOT"  # a secret key nao fica largada no diretorio do ec2-user

echo "==> [5/8] virtualenv + deps"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  sudo python3 -m venv "$APP_DIR/.venv"
fi
sudo "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> [6/8] permissoes"
sudo chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
sudo chmod 640 "$APP_DIR/.env"

echo "==> [7/8] systemd unit"
sudo cp "$SRC_DIR/$SVC_NAME.service" "/etc/systemd/system/$SVC_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SVC_NAME" >/dev/null 2>&1 || true

echo "==> [8/8] (re)start"
sudo systemctl restart "$SVC_NAME"
sleep 3
sudo systemctl --no-pager --lines=0 status "$SVC_NAME" | head -5 || true
CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")"
echo "    healthz: $CODE"
[ "$CODE" = "200" ] || { sudo journalctl -u "$SVC_NAME" -n 30 --no-pager; exit 1; }
echo "==> OK"
