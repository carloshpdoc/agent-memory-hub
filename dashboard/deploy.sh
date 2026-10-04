#!/usr/bin/env bash
# Deploy do dashboard pro EC2 via tunel SSH-over-SSM (sem porta 22 aberta).
#   1. monta um bundle: dashboard/ + lib/ (memory_client, recall_session, project_key do repo)
#   2. rsync -> ec2-user@host:/home/ec2-user/amh-dashboard-src
#   3. roda provision-remote.sh no servidor (cria user/venv/.env/systemd, (re)start)
#   4. com --expose: roda expose-cloudflare.sh (ingress + DNS) — outward-facing
#
# Na 1a vez, SUPABASE_URL/SUPABASE_SECRET_KEY do .env local vao num .env.bootstrap
# (chmod 600, pelo tunel SSM) que o provision consome e apaga.
#
# Uso:
#   bash dashboard/deploy.sh            # so o servico (interno, 127.0.0.1)
#   bash dashboard/deploy.sh --expose   # servico + exposicao Cloudflare
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# infra de cada um (instancia, chave, tunnel, hostname): fora do git, em deploy.env
# (copie deploy.env.example). Variaveis de ambiente tem precedencia.
if [[ -f "$HERE/deploy.env" ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Z_]+$ && -z "${!k:-}" ]] && export "$k=$v"
  done < <(grep -E '^[A-Z_]+=' "$HERE/deploy.env")
fi
for var in SSH_KEY SSM_INSTANCE AWS_PROFILE_DEPLOY; do
  [[ -n "${!var:-}" ]] || { echo "ERRO: $var nao definido (dashboard/deploy.env)" >&2; exit 1; }
done
KEY="$SSH_KEY"
INST="$SSM_INSTANCE"
PROFILE="$AWS_PROFILE_DEPLOY"
REGION="${AWS_REGION_DEPLOY:-us-east-1}"
USER=ec2-user
REPO="$(dirname "$HERE")"
REMOTE_SRC=/home/ec2-user/amh-dashboard-src
EXPOSE=0
[[ "${1:-}" == "--expose" ]] && EXPOSE=1

env_value() {  # le uma chave do .env local sem dar source (valores podem ter caracteres especiais)
  grep -E "^$1=" "$REPO/.env" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

STAGE="$(mktemp -d -t amh-dashboard.XXXXXX)"
WRAP="$(mktemp -t ssm-ssh.XXXXXX)"
trap 'rm -rf "$STAGE" "$WRAP"' EXIT

echo "==> bundle"
rsync -a --exclude='__pycache__/' --exclude='.venv/' --exclude='.env*' "$HERE/" "$STAGE/"
mkdir -p "$STAGE/lib"
cp "$REPO/scripts/memory_client.py" "$REPO/hooks/recall_session.py" "$REPO/hooks/project_key.py" "$STAGE/lib/"

SUPA_URL="$(env_value SUPABASE_URL)"
SUPA_KEY="$(env_value SUPABASE_SECRET_KEY)"
[[ -n "$SUPA_URL" && -n "$SUPA_KEY" ]] || { echo "ERRO: SUPABASE_URL/SUPABASE_SECRET_KEY ausentes em $REPO/.env" >&2; exit 1; }
( umask 077; printf 'SUPABASE_URL=%s\nSUPABASE_SECRET_KEY=%s\n' "$SUPA_URL" "$SUPA_KEY" > "$STAGE/.env.bootstrap" )

cat > "$WRAP" <<EOF
#!/usr/bin/env bash
exec ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 \\
  -o ProxyCommand="aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p --profile $PROFILE --region $REGION" \\
  "\$@"
EOF
chmod +x "$WRAP"

echo "==> rsync bundle -> $USER@$INST:$REMOTE_SRC/"
rsync -a --delete -e "bash $WRAP" "$STAGE/" "$USER@$INST:$REMOTE_SRC/"

echo "==> provisionando servico no servidor"
bash "$WRAP" "$USER@$INST" "bash $REMOTE_SRC/provision-remote.sh"

if [[ $EXPOSE -eq 1 ]]; then
  [[ "${TUNNEL_ID:-}" =~ ^[0-9a-f-]{36}$ ]] || { echo "ERRO: TUNNEL_ID invalido/ausente (dashboard/deploy.env)" >&2; exit 1; }
  [[ "${DASHBOARD_HOST:-}" =~ ^[a-z0-9.-]+$ ]] || { echo "ERRO: DASHBOARD_HOST invalido/ausente (dashboard/deploy.env)" >&2; exit 1; }
  echo "==> expondo via Cloudflare (ingress + DNS)"
  bash "$WRAP" "$USER@$INST" "TUNNEL_ID=$TUNNEL_ID DASHBOARD_HOST=$DASHBOARD_HOST bash $REMOTE_SRC/expose-cloudflare.sh"
fi

echo "==> concluido"
