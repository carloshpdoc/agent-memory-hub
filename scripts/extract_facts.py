#!/usr/bin/env python3
"""
agent-memory-hub — extract durable facts from sessions (Phase 4, OPTIONAL).

For each unprocessed session, asks an LLM to extract atomic, reusable facts
(preferences / decisions / configs), embeds each, dedupes against existing valid
facts in the same scope, and stores the new ones. Marks the session as processed.

This layer is OPTIONAL and bring-your-own-LLM. The core product (capture, recall,
summary, hybrid search) needs NO LLM. Pick a provider via FACTS_LLM:

  off    (default) — do nothing. Core works without facts.
  auto             — fallback chain (FACTS_CHAIN). Tries each provider in order
                     until one succeeds. Default: ollama,codex,claude,cursor.
  ollama           — local, free, private. Needs Ollama running.
  codex/claude/cursor — usa a CLI ja instalada+logada na maquina (sem API key,
                     sem servidor local). Portavel: qualquer maquina com a CLI serve.
  gemini           — Google AI Studio free tier. Needs GEMINI_API_KEY.
  openai           — OpenAI or any OpenAI-compatible endpoint (Groq, OpenRouter, local).

Config (env or ../.env):
  SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY
  FACTS_LLM (off|auto|ollama|codex|claude|cursor|gemini|openai), BATCH (4), DEDUP_SIM (0.90)
  FACTS_CHAIN (ollama,codex,claude,cursor)  — ordem do fallback quando FACTS_LLM=auto
  FACTS_CLI_TIMEOUT (240)  — timeout por chamada de CLI, em segundos
  GEMINI_API_KEY, GEMINI_MODEL (gemini-2.5-flash)
  OLLAMA_URL (http://localhost:11434), OLLAMA_MODEL (qwen2.5:7b)
  OPENAI_API_KEY, OPENAI_BASE_URL (https://api.openai.com/v1), OPENAI_MODEL (gpt-4o-mini)

Run on a cron (e.g. EC2, every 15-30 min), like embed_pending.py.

Flags:
  --loop                 keep processing batches until the pending queue is empty
  --reprocess how-to|all reset facts_extracted_at first (re-extract old sessions), then
                         loop. 'how-to' targets sessions whose content looks like a
                         procedure; 'all' re-extracts everything. Dedup keeps it safe —
                         existing facts are dropped, only genuinely new ones are stored.
                         (Friendlier: `mem reprocess [how-to|all]`.)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_PATH = os.path.join(REPO, ".env")
MAX_CONTENT = 12000
MAX_FACTS = 8

PROMPT = """You extract durable, reusable memory from a coding-assistant session transcript.
Return ONLY a JSON array (no prose, no markdown). Each element:
{{"fact": "<self-contained statement of a durable preference, decision, config, fact or procedure useful in FUTURE sessions>",
  "kind": "preference" | "decision" | "config" | "fact" | "procedure",
  "scope": "<project name if specific, else null>"}}
Rules:
- Extract 0 to {max_facts} items. Prefer fewer, higher-signal facts.
- Durable only: preferences, architectural decisions, configs, stable project/setup facts.
- "procedure" = a reusable how-to that WORKED in the session (the steps/commands to do a
  recurring task, e.g. "to deploy X: run A, then B"). Only if shown to work; keep it short.
- SKIP one-off questions, transient status, greetings, ephemeral debugging, anything not reusable.
- Each fact must be self-contained (no dangling "it"/"this").
- Write each fact in the same language as the session.
Session project: {project}

Transcript (truncated):
{content}
"""

# Supersessao temporal (fase de extracao): um fato novo parecido-mas-nao-identico a um
# existente pode ser uma ATUALIZACAO (o antigo ficou obsoleto). Em vez de acumular os
# dois e deixar o recall contraditorio, um juiz LLM decide; se for update, o antigo e
# invalidado (valid_until + superseded_by) — nao-destrutivo, o registro permanece.
SUPERSEDE_PROMPT = """Two facts about the same project scope. Does the NEW fact make the OLD one obsolete?
NEW (from the latest session): {new}
OLD (already stored): {old}
Reply ONLY JSON: {{"relation": "update" | "distinct"}}.
- "update": both describe the SAME specific thing (same config/decision/preference/item) and
  NEW is the newer state — keeping OLD would contradict NEW.
- "distinct": different things, or OLD still holds alongside NEW. Default when unsure.
"""


def parse_relation(txt):
    """Extrai {"relation": ...} da resposta do juiz; default 'distinct' (seguro)."""
    t = (txt or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        d = json.loads(t.strip())
    except json.JSONDecodeError:
        d = _json_slice(t)
    if isinstance(d, dict):
        return str(d.get("relation", "distinct")).lower()
    return "distinct"


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def http(url, headers, body=None, method="GET", timeout=60, want_headers=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if want_headers:
            return {k.lower(): v for k, v in r.headers.items()}
        return r.read()


def _json_slice(txt):
    """Extrai o primeiro array/objeto JSON de uma saida com prosa em volta
    (CLIs as vezes prefixam texto apesar do 'Return ONLY JSON')."""
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        i, j = txt.find(open_ch), txt.rfind(close_ch)
        if i != -1 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def parse_facts(txt):
    """Aceita array JSON, {facts:[...]}, ou com cercas markdown."""
    txt = (txt or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt[:4].lower() == "json":
            txt = txt[4:]
        txt = txt.strip()
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        data = _json_slice(txt)
        if data is None:
            return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("facts"), list):
            return data["facts"]
        if "fact" in data:           # modelo devolveu um objeto unico
            return [data]
    return []


# --- providers: cada um recebe (prompt, g) e devolve o texto bruto da LLM ---
def call_gemini(prompt, g):
    model = g("GEMINI_MODEL", "gemini-2.5-flash")
    key = g("GEMINI_API_KEY")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.2, "maxOutputTokens": 1024}}
    raw = http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
               {"Content-Type": "application/json"}, body, "POST")
    return json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]


def call_ollama(prompt, g):
    base = g("OLLAMA_URL", "http://localhost:11434")
    model = g("OLLAMA_MODEL", "qwen2.5:7b")
    # think:false is required for reasoning models (qwen3, etc.) — with format=json
    # the JSON grammar otherwise suppresses output. Ignored by non-thinking models.
    body = {"model": model, "prompt": prompt, "format": "json", "stream": False,
            "think": False, "options": {"temperature": 0.2}}
    raw = http(f"{base}/api/generate", {"Content-Type": "application/json"}, body, "POST", timeout=180)
    return json.loads(raw)["response"]


def call_openai(prompt, g):
    base = g("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = g("OPENAI_MODEL", "gpt-4o-mini")
    key = g("OPENAI_API_KEY")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}, "temperature": 0.2}
    raw = http(f"{base}/chat/completions",
               {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, body, "POST")
    return json.loads(raw)["choices"][0]["message"]["content"]


# --- providers CLI: usam a LLM ja instalada/logada na maquina (sem API key,
# sem servidor local). Cada um faz shell-out e devolve o texto bruto.
# AMH_NO_CAPTURE=1 impede que a propria chamada headless vire uma sessao capturada.
def _cli_env():
    e = dict(os.environ)
    e["AMH_NO_CAPTURE"] = "1"
    return e


def _cli_timeout(g):
    return int(g("FACTS_CLI_TIMEOUT", "240"))


def call_claude(prompt, g):
    exe = shutil.which("claude")
    if not exe:
        raise FileNotFoundError("claude CLI nao encontrado")
    r = subprocess.run([exe, "-p", prompt, "--output-format", "text"],
                       capture_output=True, text=True,
                       timeout=_cli_timeout(g), env=_cli_env())
    if r.returncode != 0:
        raise RuntimeError(f"claude exit {r.returncode}: {r.stderr.strip()[:200]}")
    out = r.stdout.strip()
    if not out:
        raise RuntimeError("claude devolveu saida vazia")
    return out


def call_cursor(prompt, g):
    exe = shutil.which("cursor-agent")
    if not exe:
        raise FileNotFoundError("cursor-agent CLI nao encontrado")
    r = subprocess.run([exe, "-p", prompt, "--output-format", "text"],
                       capture_output=True, text=True,
                       timeout=_cli_timeout(g), env=_cli_env())
    if r.returncode != 0:
        raise RuntimeError(f"cursor exit {r.returncode}: {r.stderr.strip()[:200]}")
    out = r.stdout.strip()
    if not out:
        raise RuntimeError("cursor devolveu saida vazia")
    return out


def call_codex(prompt, g):
    exe = shutil.which("codex")
    if not exe:
        raise FileNotFoundError("codex CLI nao encontrado")
    fd, out_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        r = subprocess.run(
            [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
             "-o", out_path, prompt],
            capture_output=True, text=True,
            timeout=_cli_timeout(g), env=_cli_env())
        if r.returncode != 0:
            raise RuntimeError(f"codex exit {r.returncode}: {r.stderr.strip()[:200]}")
        with open(out_path) as f:
            out = f.read().strip()
        if not out:
            raise RuntimeError("codex devolveu saida vazia")
        return out
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


PROVIDERS = {"gemini": call_gemini, "ollama": call_ollama, "openai": call_openai,
             "codex": call_codex, "claude": call_claude, "cursor": call_cursor}
DEFAULT_CHAIN = "ollama,codex,claude,cursor"


def pick_caller(g):
    """Resolve FACTS_LLM em UM caller (primeiro da cadeia quando 'auto').

    Usado pelos jobs de manutencao (consolidate/defrag), que precisam de um juiz
    unico — diferente do main() daqui, que tenta a cadeia inteira por sessao."""
    provider = (g("FACTS_LLM", "off") or "off").lower()
    if provider == "off":
        return None, None
    chain = ([p.strip() for p in (g("FACTS_CHAIN", DEFAULT_CHAIN) or "").split(",") if p.strip()]
             if provider == "auto" else [provider])
    for name in chain:
        if name in PROVIDERS:
            return name, PROVIDERS[name]
    return None, None


# Sinais de que uma sessao contem um how-to (procedimento) — usado pelo --reprocess.
# Heuristica grosseira de proposito: pega candidatas, o LLM decide se ha procedure.
HOW_TO_TERMS = ("deploy", "rodar", "install", "setup", "configurar", "comando",
                "rsync", "launchd", "systemctl", "cron", "build", "passo a passo")


def reset_for_reprocess(url, key, mode):
    """Zera facts_extracted_at pra reprocessar. mode: 'all' | 'how-to'. Retorna a contagem."""
    H = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal,count=exact"}
    if mode == "all":
        flt = "facts_extracted_at=not.is.null"
    else:  # how-to: content casa qualquer termo de procedimento
        ors = ",".join(f"content.ilike.*{t}*" for t in HOW_TO_TERMS)
        flt = f"or=({ors})"
    raw = http(f"{url}/rest/v1/sessions?{flt}", H,
               {"facts_extracted_at": None}, "PATCH", want_headers=True)
    # content-range: "0-147/148" -> total apos a barra
    cr = (raw or {}).get("content-range", "")
    return cr.split("/")[-1] if "/" in cr else "?"


def process_batch(g, callers, provider, url, key, ek, batch, dedup_sim, supersede_sim):
    """Processa UM batch de sessoes pendentes. Retorna (n_sessions, n_facts, n_superseded)."""
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    sel = "select=id,session_id,project,machine,content"
    sessions = json.loads(http(
        f"{url}/rest/v1/sessions?facts_extracted_at=is.null&order=started_at.desc&limit={batch}&{sel}",
        {"apikey": key, "Authorization": f"Bearer {key}"}))

    def embed(text):
        return json.loads(http(f"{url}/functions/v1/embed",
                               {"x-embed-key": ek, "Content-Type": "application/json"},
                               {"text": text}, "POST"))["embedding"]

    total = 0
    n_superseded = 0
    used_tally = {}
    for s in sessions:
        prompt = PROMPT.format(max_facts=MAX_FACTS, project=s.get("project") or "unknown",
                               content=(s.get("content") or "")[:MAX_CONTENT])
        # Cadeia de fallback: tenta cada provider na ordem ate um NAO falhar.
        # Excecao (ollama down, CLI ausente, timeout, exit!=0) => proximo provider.
        facts = judge = None
        for name, caller in callers:
            try:
                facts = parse_facts(caller(prompt, g))
                used_tally[name] = used_tally.get(name, 0) + 1
                judge = caller  # mesmo provider julga supersessao dos fatos desta sessao
                break
            except Exception as e:
                print(f"{name} falhou p/ {s['session_id']}: {type(e).__name__} {e}", file=sys.stderr)
                continue
        if facts is None:
            print(f"todos os providers falharam p/ {s['session_id']}, pulando", file=sys.stderr)
            continue
        for item in facts[:MAX_FACTS]:
            fact = (item.get("fact") or "").strip()
            if len(fact) < 8:
                continue
            scope = item.get("scope") or s.get("project")
            vec = embed(fact)
            dup = json.loads(http(f"{url}/rest/v1/rpc/match_facts", H,
                                  {"query_embedding": vec, "match_count": 1, "filter_scope": scope}, "POST"))
            if dup and dup[0].get("similarity", 0) >= dedup_sim:
                continue
            created = json.loads(http(f"{url}/rest/v1/facts", {**H, "Prefer": "return=representation"}, {
                "fact": fact, "kind": item.get("kind", "fact"), "scope": scope,
                "source_session_id": s["session_id"], "machine": s.get("machine"),
                "embedding": json.dumps(vec),
            }, "POST"))
            total += 1
            # Supersessao temporal: parecido-mas-nao-identico no MESMO scope pode ser
            # atualizacao. Juiz LLM decide; "update" invalida o antigo (nao-destrutivo).
            top = dup[0] if dup else None
            if (top and judge and created and top.get("scope") == scope
                    and supersede_sim <= top.get("similarity", 0) < dedup_sim):
                try:
                    rel = parse_relation(judge(
                        SUPERSEDE_PROMPT.format(new=fact, old=top["fact"]), g))
                except Exception:
                    rel = "distinct"
                if rel == "update":
                    http(f"{url}/rest/v1/facts?id=eq.{top['id']}",
                         {**H, "Prefer": "return=minimal"},
                         {"valid_until": datetime.now(timezone.utc).isoformat(),
                          "superseded_by": created[0]["id"]}, "PATCH")
                    n_superseded += 1
        http(f"{url}/rest/v1/sessions?id=eq.{s['id']}", {**H, "Prefer": "return=minimal"},
             {"facts_extracted_at": datetime.now(timezone.utc).isoformat()}, "PATCH")

    tally = " ".join(f"{k}={v}" for k, v in used_tally.items()) or "-"
    print(f"[{provider}] processed {len(sessions)} session(s), stored {total} new fact(s), "
          f"superseded {n_superseded} (providers: {tally})")
    return len(sessions), total, n_superseded


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    loop = "--loop" in argv
    reprocess = None
    if "--reprocess" in argv:
        i = argv.index("--reprocess")
        reprocess = argv[i + 1] if i + 1 < len(argv) else "how-to"
        if reprocess not in ("all", "how-to"):
            print(f"ERRO: --reprocess aceita 'how-to' ou 'all', nao '{reprocess}'", file=sys.stderr)
            return 2

    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    provider = (g("FACTS_LLM", "off") or "off").lower()
    if provider == "off":
        print("FACTS_LLM=off; camada de fatos desligada")
        return 0
    if provider == "auto":
        chain = [p.strip() for p in (g("FACTS_CHAIN", DEFAULT_CHAIN) or "").split(",")
                 if p.strip()]
    else:
        chain = [provider]
    callers = [(name, PROVIDERS[name]) for name in chain if name in PROVIDERS]
    if not callers:
        print(f"ERRO: nenhum provider valido em {chain}", file=sys.stderr)
        return 1

    url, key, ek = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY"), g("EMBED_KEY")
    if not all([url, key, ek]):
        print("ERRO: faltam SUPABASE_URL/SECRET_KEY/EMBED_KEY", file=sys.stderr)
        return 1
    batch = int(g("BATCH", "4"))
    dedup_sim = float(g("DEDUP_SIM", "0.90"))
    supersede_sim = float(g("SUPERSEDE_SIM", "0.75"))

    if reprocess:
        n = reset_for_reprocess(url, key, reprocess)
        print(f"[reprocess {reprocess}] {n} sessao(oes) na fila de reprocessamento")
        loop = True  # reprocessar sem --loop nao faria sentido (so um batch)

    grand = [0, 0, 0]
    while True:
        n_sess, n_facts, n_sup = process_batch(
            g, callers, provider, url, key, ek, batch, dedup_sim, supersede_sim)
        for i, v in enumerate((n_sess, n_facts, n_sup)):
            grand[i] += v
        if not loop or n_sess == 0:
            break
    if loop:
        print(f"[total] {grand[0]} sessao(oes), {grand[1]} fato(s) novo(s), {grand[2]} supersedido(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
