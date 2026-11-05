"""
agent-memory-hub — `mem continuar <agente>`: retoma no terminal, em outro agente, o trabalho
da última sessão DESTA pasta (qualquer ferramenta, capturada pelo hub).

Funciona com a ferramenta de origem sem tokens: nada roda dentro dela. O pacote sai do
transcript que o hub capturou no último fim de turno, no mesmo formato do Agent Console
(app/src/handoff.ts): resumo no topo, últimos turnos na íntegra no fim, ponteiro para o
histórico completo. O agente de destino abre interativo, com o pacote e o próximo passo
como primeira mensagem, e nos modos normais de permissão (nenhuma flag de bypass).

Filtra pela pasta (metadata.cwd), não só pelo projeto: worktrees do mesmo repo dividem o
project e trariam a sessão de outra tarefa.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
from handoff import BLOCK_RE, SUBAGENT_MARK, SYSTEM_BLOCK_RE, git_snapshot  # noqa: E402

HEADING = "# Continuação de trabalho"     # o mesmo do app: um agente reconhece os dois
NEXT_STEP = "## Próximo passo"
BUDGET = 16000
REQUEST_MAX = 1500
ENTRY_MAX = 3000

# id na linha de comando -> (tool gravado pelo hub, nome, comando interativo)
AGENTS = {
    "claude": ("claude-code", "Claude Code", ["claude"]),
    "codex": ("codex", "Codex", ["codex"]),
    "cursor": ("cursor", "Cursor", ["cursor-agent"]),
}
TOOL_LABEL = {tool: label for tool, label, _ in AGENTS.values()}


def _clip(text, n):
    t = (text or "").strip()
    return t if len(t) <= n else f"{t[:n]}… [+{len(t) - n} caracteres]"


def turns(content):
    """[(role, texto)] da conversa principal, sem blocos de sistema nem mensagens injetadas."""
    from capture_session import INJECTED_PREFIXES   # import tardio, como em handoff.py
    parts = BLOCK_RE.split((content or "").split(SUBAGENT_MARK)[0])
    out = []
    for i in range(1, len(parts) - 1, 2):
        role, text = parts[i], SYSTEM_BLOCK_RE.sub(" ", parts[i + 1]).strip()
        if not text or (role == "user" and text.lower().startswith(INJECTED_PREFIXES)):
            continue
        out.append(("Usuário" if role == "user" else "Agente", text))
    return out


def _git_line(g):
    if not isinstance(g, dict) or not g.get("head"):
        return None
    line = f"`{g.get('branch')}` @ `{g.get('head')}`"
    n = g.get("dirty_count")
    if n:
        line += f"; {n} arquivo(s) não commitado(s): " + ", ".join(f"`{f}`" for f in g.get("dirty") or [])
    elif n == 0:
        line += "; working tree limpa"
    return line


def build_pack(row, cwd, to_label, git_now=None, budget=BUDGET):
    """Pacote de continuação a partir de uma linha de `sessions` (content + metadata)."""
    from_label = TOOL_LABEL.get(row.get("tool"), row.get("tool") or "?")
    sid = row.get("session_id") or ""
    convo = turns(row.get("content"))
    users = [t for r, t in convo if r == "Usuário"]
    branch = (git_now or {}).get("branch")
    head = [
        f"{HEADING}: {from_label} → {to_label}",
        f"Você está assumindo uma sessão que começou no {from_label}. Pasta: `{cwd}`"
        + (f", branch `{branch}`" if branch else "") + ".",
        "Confira o estado real (git status, os arquivos) antes de agir: este resumo foi montado da conversa "
        "e pode estar desatualizado. Não refaça o que já está feito.",
        f"A conversa foi capturada no último fim de turno ({row.get('ended_at') or '?'}): "
        "o que aconteceu depois disso não está aqui.",
    ]
    if users:
        head += ["", "## Pedido original", _clip(users[0], REQUEST_MAX)]
    if len(users) > 1:
        head += ["", "## Último pedido", _clip(users[-1], REQUEST_MAX)]
    saved, now = _git_line((row.get("metadata") or {}).get("git")), _git_line(git_now)
    if saved or now:
        head += ["", "## Git"]
        if saved:
            head.append(f"- ao capturar: {saved}")
        if now:
            head.append(f"- agora: {now}")
    tail = ["", "## Histórico completo",
            f"Sessão de origem: {from_label}, id `{sid}`. Se precisar de algo que não está aqui, busque no "
            f"agent-memory-hub (get_session com esse id, ou `mem show {sid[:8]}`)."] if sid else []

    lines = [f"### {r}\n{_clip(t, ENTRY_MAX)}" for r, t in convo]
    used = len("\n".join(head + ["", "## Últimos turnos (na íntegra)"] + tail))
    kept = []
    for line in reversed(lines):
        if used + len(line) + 1 > budget and kept:    # o turno mais recente sempre entra
            break
        kept.insert(0, line)
        used += len(line) + 1
    omitted = len(lines) - len(kept)
    body = ([f"({omitted} item(ns) anteriores omitidos; estão no histórico completo)"] if omitted else []) + kept
    return "\n".join(head + ["", "## Últimos turnos (na íntegra)"] + body + tail)


def _repo_top(cwd):
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3)
    return r.stdout.strip() if r.returncode == 0 else None


def find_source(store, cwd, exclude_tool, project=None, branch=None):
    """A última sessão desta pasta (ou da raiz do repo, se a sessão foi aberta nela) feita
    em outra ferramenta; senão, do mesmo projeto e branch (a outra máquina, onde o caminho
    muda). Nunca o projeto inteiro: traria outra worktree, que é outra tarefa."""
    for d in dict.fromkeys(p for p in (cwd, _repo_top(cwd)) if p):
        row = store.last_activity(None, exclude_tool=exclude_tool, cwd=d)
        if row:
            return row
    if project and branch:
        return store.last_activity(project, exclude_tool=exclude_tool, branch=branch)
    return None


USAGE = ('uso: mem continuar <claude|codex|cursor> ["próximo passo"] [--print] [--sessao ID]\n'
         "  abre o agente com o contexto da última sessão desta pasta feita em outra ferramenta;\n"
         "  sem o próximo passo no comando, ele é pedido no terminal. --print só mostra o pacote.")


def main(argv):
    args, flags, sid = [], set(), None
    it = iter(argv)
    for a in it:
        if a == "--sessao":
            sid = next(it, None)
        elif a.startswith("--"):
            flags.add(a)
        else:
            args.append(a)
    if not args or args[0] not in AGENTS or "--help" in flags or (sid is None and "--sessao" in argv):
        print(USAGE, file=sys.stderr)
        return 2
    target = args[0]
    tool, label, cmd = AGENTS[target]
    cwd = os.getcwd()

    from memory_client import _S   # import tardio: --help não abre conexão
    if sid:
        row = _S.find_session(sid)
        if row:   # find_session não traz metadata/ended_at; o pacote vive sem eles
            row = {**row, "metadata": {}, "ended_at": None}
    else:
        from project_key import project_key
        from memory_client import ENV
        row = find_source(_S, cwd, tool, project_key(cwd, {**ENV, **os.environ}),
                          (git_snapshot(cwd) or {}).get("branch"))
    if not row:
        where = f"sessão {sid}" if sid else f"sessão de outra ferramenta nesta pasta ou neste branch ({cwd})"
        print(f"nenhuma {where} no hub. Veja `mem recent` e use --sessao <id>.", file=sys.stderr)
        return 1

    pack = build_pack(row, cwd, label, git_snapshot(cwd))
    if "--print" in flags:
        print(pack)
        return 0
    nxt = " ".join(args[1:]).strip()
    if not nxt and sys.stdin.isatty():
        print(f"retomando {TOOL_LABEL.get(row.get('tool'), row.get('tool'))} `{row['session_id'][:8]}` "
              f"({len(pack)} caracteres de contexto) no {label}.")
        nxt = input("próximo passo: ").strip()
    if not nxt:
        print("sem próximo passo, nada foi aberto: o agente só perguntaria o que fazer.", file=sys.stderr)
        return 1
    prompt = f"{pack}\n\n{NEXT_STEP}\n{nxt}\n"
    try:
        os.execvp(cmd[0], [*cmd, prompt])   # o agente assume o terminal
    except FileNotFoundError:
        print(f"{cmd[0]} não está no PATH.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
