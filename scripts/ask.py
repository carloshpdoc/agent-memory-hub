#!/usr/bin/env python3
"""
agent-memory-hub — `mem ask`: answer a question from the memory, citing the sessions.

Retrieval is the recall path (hybrid_search, repeated prompts collapsed), then, inside each
session found, the passages closest to the question (best_chunks, sql/14) and the durable
facts closest to it (match_facts). Only those excerpts go to the LLM, which is the same
chain fact extraction uses (FACTS_LLM / FACTS_CHAIN in .env; --llm overrides it).

Inside Claude Code / Codex / Cursor this is redundant (the agent itself answers from
recall_relevant); it is for the terminal.

  python3 scripts/ask.py [--project P] [-k N] [--llm claude] [--context] "<pergunta>"
  --context   only prints what would be sent to the LLM (no LLM call)
"""
import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory_client as mc  # noqa: E402
from extract_facts import pick_caller  # noqa: E402

TOP_SESSIONS = 5
PASSAGES_PER_SESSION = 2
FALLBACK_CHARS = 1500       # sessão ainda sem chunks: o começo dela (o tamanho de um chunk)
MAX_FACTS = 6
MIN_FACT_SIM = 0.87         # gte-small dá ~0,85 até a fato sem relação nenhuma; os úteis ficam ≥ 0,87
OVERFETCH_WORK = 3          # candidatas por vaga quando sessões de trabalho saem do contexto
ID_LEN = 8                  # o prefixo de session_id que o resto do hub mostra e o `mem show` aceita

ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

PROMPT = """You answer a developer's question using ONLY their own memory: excerpts of past
coding-assistant sessions and durable facts, below. Rules:
- Use only the context. If it does not answer the question, say so plainly; never invent.
- Cite the session id in brackets right after each claim it supports, e.g. [{example}].
- Prefer the most recent session when two of them disagree, and say they disagree.
- Be concise: a short answer, then the concrete details (commands, paths, names) that matter.
- Answer in the same language as the question.
Return ONLY JSON: {{"answer": "<the answer, markdown allowed>"}}

QUESTION: {question}

FACTS:
{facts}

SESSIONS (newest first):
{sessions}
"""


def work_terms():
    """Termos do COMMIT_DENYLIST que tiram sessões de trabalho do contexto mandado ao LLM.
    Vazio na própria máquina do trabalho (o nome dela contém um termo): lá elas são da casa."""
    raw = os.environ.get("COMMIT_DENYLIST") or mc.ENV.get("COMMIT_DENYLIST") or ""
    terms = [t.strip().lower() for t in raw.split(",") if t.strip()]
    host = socket.gethostname().lower()
    return [] if any(t in host for t in terms) else terms


def denied(row, terms):
    """Sessão/fato de trabalho: termo no projeto (ou scope) OU na máquina. Sessões do Cursor
    da máquina do trabalho chegam sem projeto, por isso a máquina também conta."""
    hay = f"{row.get('project') or row.get('scope') or ''} {row.get('machine') or ''}".lower()
    return any(t in hay for t in terms)


def retrieve(question, project=None, k=TOP_SESSIONS, terms=None):
    """{"sessions": [...], "facts": [...]}: as sessões mais relevantes (com os trechos que
    importam) e os fatos válidos mais próximos da pergunta, sem os de trabalho (work_terms)."""
    if not mc.EK:
        raise RuntimeError("EMBED_KEY ausente no .env: o mem ask precisa da busca semântica")
    terms = work_terms() if terms is None else terms
    emb = mc.embed(question)
    rows = mc.ranked_sessions(question, project, k * OVERFETCH_WORK if terms else k, embedding=emb)
    rows = [r for r in rows if not denied(r, terms)][:k]
    passages = {}
    if rows:
        for c in mc.rpc("best_chunks", {"query_embedding": emb, "p_ids": [r["id"] for r in rows],
                                        "per_session": PASSAGES_PER_SESSION}):
            passages.setdefault(c["session_fk"], []).append(c["text"])
    sessions = [{"session_id": r["session_id"], "project": r.get("project"), "tool": r.get("tool"),
                 "started_at": r.get("started_at"), "summary": r.get("summary"),
                 "passages": passages.get(r["id"]) or [(r.get("content") or "")[:FALLBACK_CHARS]]}
                for r in rows]
    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    facts = [f for f in mc.rpc("match_facts", {"query_embedding": emb, "match_count": MAX_FACTS,
                                               "filter_scope": project})
             if (f.get("similarity") or 0) >= MIN_FACT_SIM]
    if terms and facts:   # match_facts não traz a máquina de origem
        ids = ",".join(f'"{f["id"]}"' for f in facts)
        machine = {r["id"]: r.get("machine") for r in mc.rest(f"facts?select=id,machine&id=in.({ids})")}
        facts = [f for f in facts if not denied({**f, "machine": machine.get(f["id"])}, terms)]
    return {"sessions": sessions, "facts": facts}


def build_prompt(question, ctx):
    facts = "\n".join(f"- ({f.get('kind')} · {f.get('scope') or 'global'}) {f.get('fact')}"
                      for f in ctx["facts"]) or "(none)"
    blocks = []
    for s in ctx["sessions"]:
        head = (f"[{s['session_id'][:ID_LEN]}] {s.get('project') or '?'} · {s.get('tool')} · "
                f"{(s.get('started_at') or '')[:10]} — {mc._line(s.get('summary'), 200)}")
        blocks.append(head + "\n" + "\n...\n".join(p.strip() for p in s["passages"]))
    example = ctx["sessions"][0]["session_id"][:ID_LEN] if ctx["sessions"] else "ab12cd34"
    return PROMPT.format(question=question, facts=facts, example=example,
                         sessions="\n\n".join(blocks) or "(none)")


def json_object(txt):
    """O objeto JSON da saída do LLM, com ou sem prosa/cercas em volta; None se não houver.
    (O _json_slice da extração tenta array antes: pegaria uma lista DE DENTRO do objeto.)"""
    txt = (txt or "").strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i == -1 or j < i:
        return None
    try:
        data = json.loads(txt[i:j + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_answer(txt):
    """O texto da resposta: o campo answer do JSON, ou o texto cru se a CLI ignorou o formato."""
    txt = (txt or "").strip()
    data = json_object(txt)
    if isinstance(data, dict) and isinstance(data.get("answer"), str):
        return data["answer"].strip()
    return txt


def sources(ctx, answer):
    """Linhas de fonte, as citadas na resposta primeiro (marcadas com *)."""
    out = []
    for s in ctx["sessions"]:
        sid = s["session_id"][:ID_LEN]
        cited = f"[{sid}]" in answer
        out.append((not cited, f"{'*' if cited else ' '} [{sid}] {s.get('project') or '?'} · {s.get('tool')} · "
                               f"{(s.get('started_at') or '')[:10]}  {mc._line(s.get('summary'), 80)}"))
    return [line for _, line in sorted(out, key=lambda t: t[0])]


def parse_args(argv):
    opts, words, i = {"project": None, "k": TOP_SESSIONS, "llm": None, "context": False}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--project", "-k", "--llm"):
            if i + 1 >= len(argv):
                raise ValueError(f"{a} precisa de um valor")
            key = a.lstrip("-")
            opts[key] = int(argv[i + 1]) if key == "k" else argv[i + 1]
            i += 2
            continue
        if a == "--context":
            opts["context"] = True
        else:
            words.append(a)
        i += 1
    if opts["k"] < 1:
        raise ValueError("-k precisa ser >= 1")
    return " ".join(words).strip(), opts


def main(argv):
    try:
        question, opts = parse_args(argv)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    if not question:
        print('uso: mem ask [--project P] [-k N] [--llm claude|codex|ollama|...] [--context] "<pergunta>"',
              file=sys.stderr)
        return 2

    def g(key, default=None):
        if key == "FACTS_LLM" and opts["llm"]:
            return opts["llm"]
        return os.environ.get(key) or mc.ENV.get(key) or default

    caller = None
    if not opts["context"]:
        provider, caller = pick_caller(g)
        if not caller:
            print("erro: sem LLM. Defina FACTS_LLM no .env (ex.: auto) ou passe --llm claude; "
                  "--context mostra só o contexto recuperado", file=sys.stderr)
            return 1
    ctx = retrieve(question, opts["project"], opts["k"])
    if not ctx["sessions"] and not ctx["facts"]:
        print("nada na memória sobre isso" + (f" em {opts['project']}" if opts["project"] else ""))
        return 1
    prompt = build_prompt(question, ctx)
    if opts["context"]:
        print(prompt)
        return 0
    try:
        answer = parse_answer(caller(prompt, g, schema=ANSWER_SCHEMA))
    except Exception as e:  # cadeia inteira fora do ar / timeout
        print(f"erro: o LLM não respondeu ({e})", file=sys.stderr)
        return 1
    print(answer)
    print("\nfontes (mem show <id>):")
    print("\n".join(sources(ctx, answer)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
