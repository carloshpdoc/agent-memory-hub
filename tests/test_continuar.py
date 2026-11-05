"""mem continuar: pacote a partir de uma sessão capturada e escolha da sessão de origem."""
import continuar as c

CONTENT = (
    "[user]\nCorrija o login que falha com senha vazia"
    "\n\n[assistant]\nVou ler o Login.swift."
    "\n\n[user]\n<system-reminder>ruído</system-reminder>"
    "\n\n[assistant]\nCorrigi e rodei os testes: 12 passed."
    "\n\n[user]\nAgora abra o PR"
    "\n\n--- subagent abc ---\n[user]\nnão entra"
)
ROW = {"session_id": "abc12345-xyz", "tool": "claude-code", "ended_at": "2026-11-05T08:00:00Z",
       "content": CONTENT, "metadata": {"git": {"branch": "fix/login", "head": "1a2b", "dirty_count": 1, "dirty": ["Login.swift"]}}}
NOW = {"branch": "fix/login", "head": "3c4d", "dirty_count": 0, "dirty": []}


def test_pacote_resumo_turnos_e_ponteiro():
    p = c.build_pack(ROW, "/r", "Codex", NOW)
    assert p.startswith(f"{c.HEADING}: Claude Code → Codex")
    assert "branch `fix/login`" in p
    assert "## Pedido original\nCorrija o login" in p
    assert "## Último pedido\nAgora abra o PR" in p
    assert "- ao capturar: `fix/login` @ `1a2b`; 1 arquivo(s) não commitado(s): `Login.swift`" in p
    assert "- agora: `fix/login` @ `3c4d`; working tree limpa" in p
    assert "### Agente\nCorrigi e rodei os testes: 12 passed." in p
    assert "ruído" not in p and "não entra" not in p            # sistema e subagente ficam de fora
    assert "`mem show abc12345`" in p
    assert p.index("## Últimos turnos") < p.index("### Usuário\nAgora abra o PR") < p.index("## Histórico completo")


def test_orcamento_corta_antigos_e_mantem_o_mais_recente():
    content = "".join(f"\n\n[user]\npedido {i} " + "x" * 400 for i in range(40))
    p = c.build_pack({**ROW, "content": content}, "/r", "Codex", None, budget=4000)
    assert "anteriores omitidos" in p and "pedido 39" in p
    assert "### Usuário\npedido 1 " not in p


class _Store:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def last_activity(self, project=None, exclude_tool=None, cwd=None, branch=None):
        self.calls.append((project, exclude_tool, cwd, branch))
        return self.rows.get(cwd or (project, branch))


def test_origem_pela_pasta_nunca_pelo_projeto(monkeypatch):
    monkeypatch.setattr(c, "_repo_top", lambda cwd: "/repo")
    s = _Store({"/repo": ROW})
    assert c.find_source(s, "/repo/sub", "codex") is ROW          # sessão aberta na raiz do repo
    assert s.calls == [(None, "codex", "/repo/sub", None), (None, "codex", "/repo", None)]
    other_mac = _Store({("p", "fix/a"): ROW})                      # outra máquina: caminho muda, branch não
    assert c.find_source(other_mac, "/x", "codex", "p", "fix/a") is ROW
    assert c.find_source(other_mac, "/x", "codex", "p", None) is None   # sem branch: não cai para o projeto


def test_sem_proximo_passo_nao_abre_nada(monkeypatch, capsys):
    import memory_client
    monkeypatch.setattr(memory_client, "_S", _Store({}))
    monkeypatch.setattr(c, "find_source", lambda *a, **k: ROW)
    monkeypatch.setattr(c.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(c, "git_snapshot", lambda cwd: None)
    opened = []
    monkeypatch.setattr(c.os, "execvp", lambda *a: opened.append(a))
    assert c.main(["codex"]) == 1 and opened == []
    assert c.main(["codex", "abra o PR"]) is None
    cmd, argv = opened[0]
    assert cmd == "codex" and argv[1].endswith(f"{c.NEXT_STEP}\nabra o PR\n")
    assert c.main(["vim"]) == 2
