#!/usr/bin/env python3
"""
agent-memory-hub — outcome eval: does the memory make the answer better?

eval_recall measures whether the right session comes back near the top (hit@k / MRR). That
does not show the memory helps: a session can rank first and the answer still be wrong.
This eval measures the answer itself, with and without the memory, on the same questions.

  1. Cases (cached in eval-results/answer_cases.json, so reruns compare like with like):
     sessions spread across the corpus (same sampling as eval_recall --spread; repeated
     prompts and work sessions left out, the same rule as mem ask: ask.work_terms). For each one an LLM reads the end of the
     session and writes a question the developer could ask months later, with the reference
     answer and its key points. Sessions with nothing durable are skipped.
  2. Two answers per question, same LLM: WITH memory (the mem ask path: retrieval + prompt)
     and WITHOUT (the bare model, told to say when it does not know).
  3. A judge LLM grades each answer against the reference, blind to which condition it is:
     correct (1) / partial (0.5) / wrong (0) / abstained (0, said it did not know).

Report: score and verdict rates per condition, paired wins/losses with a sign test, how often
the source session was in the retrieved context, and how often the answer cited it. "wrong"
is the number to watch: a confident wrong answer costs more than an abstention.

  python3 scripts/eval_answers.py [--cases 30] [--rebuild] [--llm claude] [--workers 4] [-v]

Every call goes to the LLM chain (FACTS_LLM / --llm): with claude, ~5 calls per case.
Results: eval-results/answers-<date>.json (gitignored: they quote your sessions).
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import memory_client as mc  # noqa: E402
import ask  # noqa: E402
from eval_recall import ambiguous_queries, sample_sessions  # noqa: E402
from extract_facts import pick_caller  # noqa: E402

OUT_DIR = os.path.join(REPO, "eval-results")
CASES_PATH = os.path.join(OUT_DIR, "answer_cases.json")
DEFAULT_CASES = 30
OVERSAMPLE = 4              # sessões candidatas por caso: muitas não têm nada durável
TAIL_CHARS = 8000           # o fim da conversa principal: onde ficam o resultado e a decisão
SCORES = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "abstained": 0.0}
CONDITIONS = ("memory", "baseline")

GEN_PROMPT = """Below is the end of a past coding-assistant session of a developer.
Write ONE question this developer could plausibly ask months later whose answer is in this
excerpt: a concrete outcome, decision (with its reason), command, path, value or fix.
- Ask it the way they would, without the excerpt at hand: do not quote its distinctive wording,
  do not mention "the session" or "the excerpt".
- It must be answerable from the excerpt alone and not be general knowledge.
- Same language as the excerpt.
If the excerpt has nothing durable (chit-chat, unfinished work, pure status), return {{"skip": true}}.
Return ONLY JSON: {{"question": "...", "answer": "<reference answer, 1-3 sentences>",
"key_points": ["<fact the answer must contain>", "..."]}}

Project: {project}
Excerpt:
{excerpt}
"""

BASELINE_PROMPT = """Answer this question from a developer about their own past work.
You have no notes or history of their work; answer only if you actually know.
If you do not know, say so plainly; never invent. Answer in the same language as the question.
Return ONLY JSON: {{"answer": "..."}}

QUESTION: {question}
"""

JUDGE_PROMPT = """Grade a candidate answer against the reference answer.
QUESTION: {question}
REFERENCE: {reference}
KEY POINTS: {key_points}
CANDIDATE: {candidate}

Verdict:
- "correct": contains the key points (wording may differ) and contradicts none of them.
- "partial": some key points, no contradiction.
- "wrong": contradicts the reference, or covers none of the key points while giving a
  substantive answer. Extra true details beyond the reference are not errors by themselves.
- "abstained": says it does not know / has no information, without a wrong claim.
Return ONLY JSON: {{"verdict": "correct" | "partial" | "wrong" | "abstained"}}
"""


# ---- casos ---------------------------------------------------------------------------------
def parse_case(txt):
    data = ask.json_object(txt)
    if not data or data.get("skip"):
        return None
    q, a = (data.get("question") or "").strip(), (data.get("answer") or "").strip()
    kp = [str(k).strip() for k in data.get("key_points") or [] if str(k).strip()]
    return {"question": q, "answer": a, "key_points": kp} if q and a else None


def build_cases(n, caller, g, terms, workers, log):
    skip = ambiguous_queries(r.get("summary") for r in
                             mc.rest_all("sessions?select=summary&summary=not.is.null"))
    rows = [r for r in sample_sessions(n * OVERSAMPLE, spread=True, skip=skip)
            if not ask.denied(r, terms)]

    def one(r):
        tail = mc.rpc("session_main_tail", {"p_session_id": r["session_id"], "p_chars": TAIL_CHARS})
        if not tail or len(tail.strip()) < 500:
            return None
        try:
            case = parse_case(caller(GEN_PROMPT.format(project=r.get("project"), excerpt=tail), g))
        except Exception as e:
            log(f"  gen falhou {r['session_id'][:8]}: {e}")
            return None
        return case and {**case, "session_id": r["session_id"], "project": r.get("project")}

    cases = []
    with ThreadPoolExecutor(workers) as ex:     # em ordem: a amostra é a mesma a cada --rebuild
        for case in ex.map(one, rows):
            if case and len(cases) < n:
                cases.append(case)
    return cases


# ---- respostas e julgamento ----------------------------------------------------------------
def answer_with_memory(question, caller, g):
    ctx = ask.retrieve(question)
    text = ask.parse_answer(caller(ask.build_prompt(question, ctx), g, schema=ask.ANSWER_SCHEMA))
    return text, [s["session_id"] for s in ctx["sessions"]]


def answer_baseline(question, caller, g):
    return ask.parse_answer(caller(BASELINE_PROMPT.format(question=question), g, schema=ask.ANSWER_SCHEMA))


def parse_verdict(txt):
    v = (ask.json_object(txt) or {}).get("verdict")
    return v if v in SCORES else None


def judge(case, candidate, caller, g):
    prompt = JUDGE_PROMPT.format(question=case["question"], reference=case["answer"],
                                 key_points="; ".join(case["key_points"]) or "-", candidate=candidate)
    return parse_verdict(caller(prompt, g))


def run_case(case, caller, g):
    out = {"session_id": case["session_id"], "question": case["question"]}
    try:
        mem, retrieved = answer_with_memory(case["question"], caller, g)
        base = answer_baseline(case["question"], caller, g)
        out.update(memory=mem, baseline=base,
                   source_retrieved=case["session_id"] in retrieved,
                   source_cited=f"[{case['session_id'][:ask.ID_LEN]}]" in mem,
                   verdict_memory=judge(case, mem, caller, g),
                   verdict_baseline=judge(case, base, caller, g))
    except Exception as e:   # um caso que falha não derruba o eval; conta como erro
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---- relatório -----------------------------------------------------------------------------
def sign_test(wins, losses):
    """p bilateral do teste do sinal (empates fora)."""
    n = wins + losses
    if not n:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def summarize(results):
    ok = [r for r in results if not r.get("error") and r.get("verdict_memory") and r.get("verdict_baseline")]
    n = len(ok) or 1
    out = {"cases": len(results), "graded": len(ok), "errors": len(results) - len(ok)}
    for c in CONDITIONS:
        vs = [r[f"verdict_{c}"] for r in ok]
        out[c] = {"score": sum(SCORES[v] for v in vs) / n,
                  **{v: vs.count(v) / n for v in SCORES}}
    diffs = [SCORES[r["verdict_memory"]] - SCORES[r["verdict_baseline"]] for r in ok]
    wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    out["paired"] = {"wins": wins, "losses": losses, "ties": len(diffs) - wins - losses,
                     "p": sign_test(wins, losses)}
    out["source_retrieved"] = sum(r["source_retrieved"] for r in ok) / n
    out["source_cited"] = sum(r["source_cited"] for r in ok) / n
    return out


def report(s, llm):
    pct = lambda x: f"{100 * x:5.1f}%"  # noqa: E731
    print(f"\neval de resultado · {s['graded']} casos avaliados ({s['errors']} com erro) · LLM: {llm}")
    print(f"  {'':<10} {'score':>6} {'certa':>7} {'parcial':>7} {'errada':>7} {'não sabe':>8}")
    for c, label in (("memory", "com hub"), ("baseline", "sem hub")):
        m = s[c]
        print(f"  {label:<10} {m['score']:6.2f} {pct(m['correct']):>7} {pct(m['partial']):>7} "
              f"{pct(m['wrong']):>7} {pct(m['abstained']):>8}")
    p = s["paired"]
    print(f"  pareado: hub melhor em {p['wins']}, pior em {p['losses']}, empate em {p['ties']} "
          f"(teste do sinal p={p['p']:.3g})")
    print(f"  sessão de origem no contexto: {pct(s['source_retrieved']).strip()} · citada na resposta: "
          f"{pct(s['source_cited']).strip()}")


def main(argv):
    n, workers, llm, rebuild, verbose = DEFAULT_CASES, 4, None, "--rebuild" in argv, "-v" in argv
    try:
        if "--cases" in argv:
            n = int(argv[argv.index("--cases") + 1])
        if "--workers" in argv:
            workers = int(argv[argv.index("--workers") + 1])
        if "--llm" in argv:
            llm = argv[argv.index("--llm") + 1]
    except (IndexError, ValueError):
        print("uso: eval_answers.py [--cases N] [--rebuild] [--llm L] [--workers W] [-v]", file=sys.stderr)
        return 2
    if n < 1 or workers < 1:
        print("--cases e --workers precisam ser >= 1", file=sys.stderr)
        return 2

    def g(key, default=None):
        if key == "FACTS_LLM" and llm:
            return llm
        return os.environ.get(key) or mc.ENV.get(key) or default

    provider, caller = pick_caller(g)
    if not caller:
        print("sem LLM: defina FACTS_LLM no .env ou passe --llm claude", file=sys.stderr)
        return 1
    log = (lambda m: print(m, file=sys.stderr, flush=True)) if verbose else (lambda m: None)
    terms = ask.work_terms()
    os.makedirs(OUT_DIR, exist_ok=True)

    cases = None
    if not rebuild and os.path.exists(CASES_PATH):
        cases = json.load(open(CASES_PATH))
        if len(cases) < n:
            print(f"cache tem {len(cases)} casos (< {n}); use --rebuild para gerar de novo", file=sys.stderr)
        cases = cases[:n]
    if not cases:
        print(f"gerando {n} casos (o LLM lê o fim de cada sessão e escreve a pergunta)...", flush=True)
        cases = build_cases(n, caller, g, terms, workers, log)
        if not cases:
            print("nenhum caso gerado", file=sys.stderr)
            return 1
        with open(CASES_PATH, "w") as f:
            json.dump(cases, f, ensure_ascii=False, indent=1)
    print(f"respondendo {len(cases)} perguntas com e sem o hub e julgando...", flush=True)

    def run(case):
        r = run_case(case, caller, g)
        log(f"  {case['session_id'][:8]}  hub={r.get('verdict_memory')}  sem={r.get('verdict_baseline')}"
            f"{'  ERRO ' + r['error'] if r.get('error') else ''}")
        return r

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(run, cases))
    s = summarize(results)
    path = os.path.join(OUT_DIR, f"answers-{date.today().isoformat()}.json")
    with open(path, "w") as f:
        json.dump({"llm": provider if not llm else llm, "summary": s, "results": results},
                  f, ensure_ascii=False, indent=1)
    report(s, llm or provider)
    print(f"  detalhes: {os.path.relpath(path, REPO)}")
    return 0 if s["graded"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
