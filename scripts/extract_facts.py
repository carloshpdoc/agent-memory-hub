#!/usr/bin/env python3
"""
agent-memory-hub — extract durable facts from sessions (Phase 4, OPTIONAL).

For each unprocessed session, asks an LLM to extract atomic, reusable facts
(preferences / decisions / configs), embeds each, dedupes against existing valid
facts in the same scope, and stores the new ones. Marks the session as processed.

This layer is OPTIONAL and bring-your-own-LLM. The core product (capture, recall,
summary, hybrid search) needs NO LLM. Pick a provider via FACTS_LLM:

  off    (default) — do nothing. Core works without facts.
  ollama           — local, free, private. Needs Ollama running.
  gemini           — Google AI Studio free tier. Needs GEMINI_API_KEY.
  openai           — OpenAI or any OpenAI-compatible endpoint (Groq, OpenRouter, local).

Config (env or ../.env):
  SUPABASE_URL, SUPABASE_SECRET_KEY, EMBED_KEY
  FACTS_LLM (off|ollama|gemini|openai), BATCH (4), DEDUP_SIM (0.90)
  GEMINI_API_KEY, GEMINI_MODEL (gemini-2.5-flash)
  OLLAMA_URL (http://localhost:11434), OLLAMA_MODEL (qwen2.5:7b)
  OPENAI_API_KEY, OPENAI_BASE_URL (https://api.openai.com/v1), OPENAI_MODEL (gpt-4o-mini)

Run on a cron (e.g. EC2, every 15-30 min), like embed_pending.py.
"""
import json
import os
import sys
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
{{"fact": "<self-contained statement of a durable preference, decision, config or fact useful in FUTURE sessions>",
  "kind": "preference" | "decision" | "config" | "fact",
  "scope": "<project name if specific, else null>"}}
Rules:
- Extract 0 to {max_facts} items. Prefer fewer, higher-signal facts.
- Durable only: preferences, architectural decisions, configs, stable project/setup facts.
- SKIP one-off questions, transient status, greetings, ephemeral debugging, anything not reusable.
- Each fact must be self-contained (no dangling "it"/"this").
- Write each fact in the same language as the session.
Session project: {project}

Transcript (truncated):
{content}
"""


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


def http(url, headers, body=None, method="GET", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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


PROVIDERS = {"gemini": call_gemini, "ollama": call_ollama, "openai": call_openai}


def main():
    env = load_env(ENV_PATH)
    def g(k, d=None):
        return os.environ.get(k) or env.get(k) or d

    provider = (g("FACTS_LLM", "off") or "off").lower()
    if provider == "off" or provider not in PROVIDERS:
        print("FACTS_LLM=off (ou nao configurado); camada de fatos desligada")
        return 0
    caller = PROVIDERS[provider]

    url, key, ek = g("SUPABASE_URL"), g("SUPABASE_SECRET_KEY"), g("EMBED_KEY")
    if not all([url, key, ek]):
        print("ERRO: faltam SUPABASE_URL/SECRET_KEY/EMBED_KEY", file=sys.stderr)
        return 1
    batch = int(g("BATCH", "4"))
    dedup_sim = float(g("DEDUP_SIM", "0.90"))

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
    for s in sessions:
        prompt = PROMPT.format(max_facts=MAX_FACTS, project=s.get("project") or "unknown",
                               content=(s.get("content") or "")[:MAX_CONTENT])
        try:
            facts = parse_facts(caller(prompt, g))
        except Exception as e:
            print(f"{provider} falhou p/ {s['session_id']}: {type(e).__name__} {e}", file=sys.stderr)
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
            http(f"{url}/rest/v1/facts", {**H, "Prefer": "return=minimal"}, {
                "fact": fact, "kind": item.get("kind", "fact"), "scope": scope,
                "source_session_id": s["session_id"], "machine": s.get("machine"),
                "embedding": json.dumps(vec),
            }, "POST")
            total += 1
        http(f"{url}/rest/v1/sessions?id=eq.{s['id']}", {**H, "Prefer": "return=minimal"},
             {"facts_extracted_at": datetime.now(timezone.utc).isoformat()}, "PATCH")

    print(f"[{provider}] processed {len(sessions)} session(s), stored {total} new fact(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
