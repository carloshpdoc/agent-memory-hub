# agent-memory-hub

> Read in [English](README.md).

Memória **persistente e compartilhada para agentes de IA** de código (Claude Code, e qualquer
ferramenta com MCP/REST), guardada no **seu próprio Supabase**. Toda sessão é salva
automaticamente numa tabela Postgres e recuperada no início da próxima, atravessando
**sessões, instâncias e máquinas**.

Self-hosted no seu projeto Supabase. Sem SaaS no meio. Seus dados, seus backups.

## Por quê

O Claude Code começa cada sessão do zero. Ferramentas como claude-mem ou mem0 resolvem
isso, mas ou guardam local (sem cross-máquina) ou passam por um serviço hospedado. O
`agent-memory-hub` é a versão mais simples e self-owned: uma tabela no Supabase, três hooks
e um backup opcional.

- **Cross-sessão:** abre amanhã, lembra de hoje.
- **Cross-instância:** vários configs do Claude Code compartilham a mesma memória.
- **Cross-máquina:** qualquer máquina apontando pro mesmo Supabase compartilha tudo.
- **Seu:** é Postgres puro. `pg_dump` quando quiser, zero lock-in.

## Como funciona

```
  SessionStart   -> recall_session.py    -> injeta um resumo das sessões anteriores relevantes
  ...sessão...
  Stop (a cada turno) -> capture_session.py -> checkpoint contínuo (background, upsert)
  SessionEnd     -> capture_session.py    -> salvamento final
                       |
                       v
            Supabase (seu projeto): tabela public.sessions
                       |
   backup opcional ....v.... pg_dump (cron num host always-on) -> .sql.gz -> puxa pro local
```

- A captura é **idempotente** (upsert por `session_id`). O checkpoint do `Stop` faz com que
  até um kill abrupto preserve a sessão até o último turno.
- O recall injeta só um **resumo compacto** (previews truncados). Os transcripts completos
  ficam disponíveis sob demanda via Supabase MCP ou REST.

## Requisitos

- [Claude Code](https://claude.com/claude-code)
- Um projeto [Supabase](https://supabase.com) (free tier)
- `python3` (hooks e backup são stdlib puro, sem pip)

## Começando (e: como configurar em outra máquina)

### 1. Clone
```bash
git clone https://github.com/carloshpdoc/agent-memory-hub.git
cd agent-memory-hub
```

### 2. Crie um projeto Supabase
Em [supabase.com](https://supabase.com): novo projeto. Ative **Data API** e **RLS**.
Pegue em **Settings > API**: Project URL, publishable key, secret key.

### 3. Aplique o schema
Abra o **SQL Editor** no Supabase e rode [`sql/01-schema.sql`](sql/01-schema.sql).
Ele cria a tabela `sessions`, o índice full-text e o RLS.

### 4. Configure o `.env`
```bash
cp .env.example .env
# edite o .env com seu SUPABASE_URL e SUPABASE_SECRET_KEY (e as vars de backup, se usar)
```
O `.env` é gitignored. Os hooks leem dele direto.

### 5. Ligue os hooks no Claude Code
Adicione no seu `settings.json` (`~/.claude/settings.json` para escopo user), usando o
**caminho absoluto** do seu clone:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "python3 /CAMINHO/ABS/agent-memory-hub/hooks/recall_session.py", "timeout": 15 }
      ]}
    ],
    "Stop": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "payload=$(cat); echo \"$payload\" | python3 /CAMINHO/ABS/agent-memory-hub/hooks/capture_session.py >/dev/null 2>&1 &" }
      ]}
    ],
    "SessionEnd": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "python3 /CAMINHO/ABS/agent-memory-hub/hooks/capture_session.py", "timeout": 20 }
      ]}
    ]
  }
}
```
> Se você já tem hooks nesses eventos, **acrescente** estas entradas aos arrays existentes.

### 6. (opcional) Adicione o Supabase MCP
Deixa o agente consultar as memórias de forma interativa:
```bash
claude mcp add --scope user --transport http supabase \
  "https://mcp.supabase.com/mcp?project_ref=<SEU_PROJECT_REF>"
# depois autentique: /mcp > supabase
```

### 7. (opcional) Módulo de backup
Num host always-on com `pg_dump` igual ou acima da versão major do seu Postgres, e o repo clonado:
- Coloque as credenciais do pooler no `~/.pgpass` (chmod 600):
  `HOST:5432:postgres:postgres.<PROJECT_REF>:SENHA`
- Preencha as vars `PG_POOLER_*` no `.env`.
- Cron: `30 3 * * * /CAMINHO/ABS/agent-memory-hub/scripts/backup.sh >> .../backup.log 2>&1`
- Puxe cópias pro local com `scripts/pull-backups.sh` (defina `REMOTE_SSH` e `SSH_KEY` no `.env`).

## Configurando em outra máquina

É o ponto central, e é trivial:

1. Clone o repo na nova máquina.
2. Copie o **mesmo `.env`** (mesmas credenciais Supabase).
3. Aplique o bloco de hooks no `settings.json` daquela máquina (igual ao passo 5).

Pronto. Essa máquina passa a gravar e ler na **mesma memória compartilhada**. Não precisa
rodar o schema de novo, a tabela já existe no seu Supabase.

## Referência de configuração

| Var | Usada por | Significado |
|-----|-----------|-------------|
| `SUPABASE_URL` | hooks, backup.py | `https://<ref>.supabase.co` |
| `SUPABASE_SECRET_KEY` | hooks | service_role key (escreve, ignora RLS) |
| `PG_POOLER_HOST`, `PG_POOLER_USER` | backup.sh | host do Session Pooler, `postgres.<ref>` |
| `BACKUP_DIR`, `KEEP` | backup.sh, backup.py | diretório de saída, quantos manter |
| `REMOTE_SSH`, `SSH_KEY` | pull-backups.sh | host always-on, chave SSH |
| `EMBED_KEY` | embed_pending.py, search.py | guard da função de embeddings (Fase 2) |

## Consultando sua memória

- **MCP:** peça ao agente. Ele roda SQL via Supabase MCP.
- **REST full-text:** `GET /rest/v1/sessions?content_tsv=fts(simple).<termo>` com a secret key.
- **Filtros:** por `project`, `machine`, `started_at`, `session_id`.

## Busca semântica (Fase 2)

Opcional. Adiciona recall por significado em cima do full-text, usando `pgvector` e o modelo
`gte-small` rodando dentro de uma Supabase Edge Function (grátis, sem API externa).

1. Rode [`sql/02-phase2-pgvector.sql`](sql/02-phase2-pgvector.sql). Adiciona a coluna
   `embedding`, o índice HNSW e a RPC `match_sessions`.
2. Defina um segredo de guard e faça deploy da função:
   ```bash
   supabase secrets set EMBED_KEY=$(openssl rand -hex 24)
   supabase functions deploy embed --no-verify-jwt   # as keys novas não são JWT
   ```
   Coloque a mesma `EMBED_KEY` no seu `.env`.
3. Embede as linhas existentes: `python3 scripts/embed_pending.py`. Rode num cron pra manter
   novas sessões embedadas (ex.: `*/15 * * * *` no seu host always-on).
4. Busque: `python3 scripts/search.py "como configuramos o backup"`.

A Edge Function devolve só vetores e contadores, nunca o conteúdo das sessões.

## Segurança

- Segredos só no `.env` e `~/.pgpass` (gitignored, chmod 600). Nunca commite.
- RLS ligado. A key pública (anon) não lê sem política. Os hooks usam a secret key.
- A secret key é poderosa. Trate como senha.

## Licença

[MIT](LICENSE)

## Star, compartilhe, contribua

Se isso te poupou de re-explicar seu projeto pro agente pela décima vez hoje, dá uma star no
repo. Ajuda de verdade outras pessoas a encontrarem.

Ideias, arestas, ou uma Fase 3 que você quer? Abra uma issue ou um pull request. Se você
construir algo em cima do agent-memory-hub, vou adorar ver.

## Feito por

Feito por **[buildcomcarlos.com](https://buildcomcarlos.com)**: artigos e ferramentas open
sobre agentes de IA, iOS e shipar software sozinho. Se esse projeto foi útil, o site é onde
ficam os deep dives e os próximos experimentos. Aparece lá.
