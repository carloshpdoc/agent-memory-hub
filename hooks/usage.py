#!/usr/bin/env python3
"""
agent-memory-hub — token usage of a session, and its API-equivalent cost.

Stored per session in sessions.usage (sql/15) as tokens per model, never as dollars:
  {"models": {"<model>": {"input": n, "cache_write_5m": n, "cache_write_1h": n,
                          "cache_read": n, "output": n}}}
so a price change only touches PRICES below, not the stored rows.

  Claude Code  every assistant message carries message.usage; a message spans several
               transcript lines with the same usage, so it is counted once per message id.
               Subagent transcripts (<session>/subagents/*.jsonl) count toward the session.
  Codex        the last token_count event holds the session's cumulative total;
               input_tokens includes the cached part, output_tokens includes reasoning.
  Cursor       its local store almost never fills token counts: no usage, reported as such.

Cost is what the same tokens would cost on the public API. On a subscription it is not
what you pay; it is the common unit to compare tasks and tools.
"""
import glob
import json
import os

FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# USD por milhão de tokens: (input, cache write 5m, cache write 1h, cache read, output).
# Fontes, conferidas em 2026-10-14:
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/models/gpt-5.5  (OpenAI não cobra escrita de cache)
# Chave = prefixo do id do modelo; o prefixo mais longo vence (claude-opus-5-5 antes de claude-opus-5).
PRICES = {
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50),
    "claude-fable-5": (10, 12.5, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.20, 20),
    "claude-opus-5": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-8": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-6": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-5": (5, 6.25, 10, 0.50, 25),
    "claude-sonnet-5-5": (2, 2.5, 4, 0.10, 10),
    "claude-sonnet-5": (2, 2.5, 4, 0.20, 10),
    "claude-sonnet-4": (3, 3.75, 6, 0.30, 15),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),   # prompts até 100k tokens
    "claude-haiku-4-5": (1, 1.25, 2, 0.10, 5),
    "gpt-5.5": (5, 0, 0, 0.50, 30),
    "gpt-5.4-mini": (0.75, 0, 0, 0.075, 4.5),
    "gpt-5.4": (2.5, 0, 0, 0.25, 15),
}


def _add(models, model, **tokens):
    m = models.setdefault(model, dict.fromkeys(FIELDS, 0))
    for k, v in tokens.items():
        m[k] += int(v or 0)


def _claude_file(path, models, seen):
    try:
        f = open(path)
    except OSError:
        return
    with f:
        for raw in f:
            if '"usage"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") or {}
            u, model, mid = msg.get("usage"), msg.get("model"), msg.get("id")
            if entry.get("type") != "assistant" or not u or not model or model.startswith("<"):
                continue    # <synthetic>: mensagem gerada localmente, sem chamada à API
            if mid in seen:
                continue
            seen.add(mid)
            cc = u.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens") or 0
            w5m = cc.get("ephemeral_5m_input_tokens")
            if w5m is None:     # transcript antigo, sem o detalhe por TTL: tudo como 5m
                w5m = (u.get("cache_creation_input_tokens") or 0) - w1h
            _add(models, model, input=u.get("input_tokens"), cache_write_5m=w5m, cache_write_1h=w1h,
                 cache_read=u.get("cache_read_input_tokens"), output=u.get("output_tokens"))


def claude_usage(path):
    """Uso do transcript principal + subagentes, ou None se não houver nenhuma chamada."""
    models, seen = {}, set()
    _claude_file(path, models, seen)
    for sub in sorted(glob.glob(os.path.join(os.path.splitext(path)[0], "subagents", "*.jsonl"))):
        _claude_file(sub, models, seen)
    return {"models": models} if models else None


def codex_usage(path):
    """Total acumulado do último token_count do rollout, no modelo da sessão; ou None."""
    model, total = None, None
    try:
        f = open(path)
    except OSError:
        return None
    with f:
        for raw in f:
            if '"turn_context"' not in raw and '"token_count"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            p = d.get("payload") or {}
            if d.get("type") == "turn_context" and p.get("model"):
                model = p["model"]
            elif p.get("type") == "token_count":
                total = ((p.get("info") or {}).get("total_token_usage")) or total
    if not total:
        return None
    cached = total.get("cached_input_tokens") or 0
    models = {}
    _add(models, model or "codex", input=(total.get("input_tokens") or 0) - cached,
         cache_read=cached, cache_write_5m=total.get("cache_write_input_tokens"),
         output=total.get("output_tokens"))
    return {"models": models}


def price_for(model):
    hits = [k for k in PRICES if (model or "").startswith(k)]
    return PRICES[max(hits, key=len)] if hits else None


def cost_usd(usage):
    """(custo em USD equivalente de API, modelos sem preço conhecido)."""
    total, unpriced = 0.0, []
    for model, t in ((usage or {}).get("models") or {}).items():
        p = price_for(model)
        if not p:
            unpriced.append(model)
            continue
        total += sum(t.get(f, 0) * rate for f, rate in zip(FIELDS, p)) / 1e6
    return total, unpriced


def tokens(usage):
    """Total de tokens (todas as categorias) de um uso."""
    return sum(sum(t.get(f, 0) for f in FIELDS) for t in ((usage or {}).get("models") or {}).values())
