import { describe, expect, it } from "vitest";
import { Mailbox, MAX_ACTIVE, MAX_NUDGES, Partner, PARTNER_SERVER, partnerServerEntry, TERMINAL_SERVER, terminalCall, toolsFor, underApp, type PartnerDeps } from "../src/partner";

/** Sessões de mentira: registra o que o Partner manda e quem está ocupado. */
function harness(mail?: Mailbox) {
  const prompts: { key: string; text: string }[] = [];
  const notices: { key: string; text: string }[] = [];
  const busy = new Set<string>();
  let n = 0;
  const deps: PartnerDeps = {
    open: async (agent) => { if (agent === "quebrado") throw new Error("não abriu quebrado"); return `w${++n}`; },
    label: (k) => (k.startsWith("w") ? `Codex ${k}` : `Claude ${k}`),
    cwd: () => "/r",
    busy: (k) => busy.has(k),
    prompt: (key, text) => { prompts.push({ key, text }); if (mail && key.startsWith("t-")) mail.deliver(key, text); },
    notice: (key, text) => notices.push({ key, text }),
  };
  return { p: new Partner(deps), prompts, notices, busy };
}

const TASK = { agente: "codex", tarefa: "Rode os testes de Login", pronto_quando: "swift test passa", pode_editar: ["Sources/Login"] };

describe("parceiro", () => {
  it("coordenador e worker veem ferramentas diferentes", () => {
    expect(toolsFor("coordenador").map((t) => t.name)).toEqual(["delegar", "responder"]);
    expect(toolsFor("worker").map((t) => t.name)).toEqual(["concluir", "perguntar"]);
  });

  it("delega com tarefa autossuficiente e acorda o coordenador com o resultado", async () => {
    const { p, prompts, busy } = harness();
    busy.add("c1");                                            // o coordenador ainda está no turno
    const r = await p.call("c1", "delegar", TASK);
    expect(r.ok).toBe(true);
    expect(r.text).toMatch(/Encerre o seu turno/);
    expect(prompts[0].key).toBe("w1");
    expect(prompts[0].text).toMatch(/## Pronto quando\nswift test passa/);
    expect(prompts[0].text).toContain("- Sources/Login");
    expect(p.roleOf("w1")).toBe("worker");

    const f = await p.call("w1", "concluir", { status: "feito", resumo: "passou", evidencias: ["swift test: 12 passed"] });
    expect(f.ok).toBe(true);
    expect(prompts).toHaveLength(1);                           // coordenador ocupado: fica na fila
    busy.delete("c1");
    p.onStop("c1", "end_turn");                                // acordado no fim do turno, não por polling
    expect(prompts[1]).toMatchObject({ key: "c1" });
    expect(prompts[1].text).toMatch(/Resultado da delegação d1 \(Codex w1\): feito/);
    expect(prompts[1].text).toContain("- swift test: 12 passed");
    p.onStop("w1", "end_turn");                                // worker reportou: sem lembrete
    expect(prompts).toHaveLength(2);
    expect(p.list()[0].state).toBe("concluida");
  });

  it("feito sem evidência é recusado; sem pode_editar a tarefa é só leitura", async () => {
    const { p, prompts } = harness();
    await p.call("c1", "delegar", { ...TASK, pode_editar: [] });
    expect(prompts[0].text).toContain("Só leitura");
    const r = await p.call("w1", "concluir", { status: "feito", resumo: "ok", evidencias: [] });
    expect(r).toMatchObject({ ok: false });
    expect(p.list()[0].state).toBe("trabalhando");
  });

  it("pergunta e resposta vão por mensagem, nas duas direções", async () => {
    const { p, prompts } = harness();
    await p.call("c1", "delegar", TASK);
    await p.call("w1", "perguntar", { pergunta: "Qual esquema?" });
    expect(prompts[1]).toMatchObject({ key: "c1" });
    expect(prompts[1].text).toContain('delegacao: "d1"');
    expect((await p.call("c2", "responder", { delegacao: "d1", resposta: "x" })).ok).toBe(false);   // outro coordenador
    const r = await p.call("c1", "responder", { delegacao: "d1", resposta: "Acme" });
    expect(r.ok).toBe(true);
    expect(prompts[2]).toMatchObject({ key: "w1" });
    expect(prompts[2].text).toContain("Acme");
    expect((await p.call("c1", "responder", { delegacao: "d1", resposta: "de novo" })).ok).toBe(false);
  });

  it("fim de turno sem concluir não prova nada: lembra e desiste depois do limite", async () => {
    const { p, prompts } = harness();
    await p.call("c1", "delegar", TASK);
    for (let i = 1; i < MAX_NUDGES; i++) {
      p.onStop("w1", "end_turn");
      expect(prompts[prompts.length - 1]).toMatchObject({ key: "w1" });
      expect(prompts[prompts.length - 1].text).toContain(`Tentativa ${i + 1} de ${MAX_NUDGES}`);
    }
    p.onStop("w1", "end_turn");
    expect(prompts[prompts.length - 1]).toMatchObject({ key: "c1" });
    expect(prompts[prompts.length - 1].text).toMatch(/falhou: encerrou 3 turnos sem chamar concluir/);
    expect(p.list()[0].state).toBe("falhou");
  });

  it("worker que morre ou é cancelado falha a delegação na hora", async () => {
    const { p, prompts } = harness();
    await p.call("c1", "delegar", TASK);
    await p.call("c1", "delegar", TASK);
    p.onGone("w1", "o processo do agente encerrou");
    p.onStop("w2", "cancelled");
    expect(prompts.filter((m) => m.key === "c1").map((m) => m.text.slice(0, 22))).toEqual(["A delegação d1 (Codex ", "A delegação d2 (Codex "]);
  });

  it("recusa: worker não delega, agente que não abre, limite de delegações abertas", async () => {
    const { p } = harness();
    await p.call("c1", "delegar", TASK);
    expect((await p.call("w1", "delegar", TASK)).text).toMatch(/não está disponível/);
    expect((await p.call("c1", "concluir", { status: "feito", resumo: "x", evidencias: ["y"] })).ok).toBe(false);
    expect(await p.call("c9", "delegar", { ...TASK, agente: "quebrado" })).toEqual({ ok: false, text: "não abriu quebrado" });
    for (let i = 1; i < MAX_ACTIVE; i++) await p.call("c1", "delegar", TASK);
    expect((await p.call("c1", "delegar", TASK)).text).toMatch(/delegações em andamento/);
    expect((await p.call("c1", "delegar", { agente: "codex", tarefa: "", pronto_quando: "" })).ok).toBe(false);
  });

  it("terminal: delegar espera o resultado na mesma chamada", async () => {
    const mail = new Mailbox();
    const { p, prompts } = harness(mail);
    expect(toolsFor("terminal").map((t) => t.name)).toEqual(["delegar", "responder", "aguardar"]);
    expect(p.roleOf("t-1")).toBe("terminal");
    const call = terminalCall(p, mail, "t-1", "delegar", TASK, 5000);
    await new Promise((r) => setTimeout(r, 0));
    expect(prompts[0]).toMatchObject({ key: "w1" });                       // worker recebeu a tarefa
    await p.call("w1", "concluir", { status: "feito", resumo: "passou", evidencias: ["12 passed"] });
    const r = await call;
    expect(r.ok).toBe(true);
    expect(r.text).toMatch(/^Delegação d1 aberta em Codex w1\. Esperando o resultado\.\n\nResultado da delegação d1/);
    expect(r.text).not.toMatch(/Encerre o seu turno/);
  });

  it("terminal: prazo esgotado devolve 'aguardar', que pega o que chegou depois", async () => {
    const mail = new Mailbox();
    const { p } = harness(mail);
    const r1 = await terminalCall(p, mail, "t-1", "delegar", TASK, 10);
    expect(r1.text).toMatch(/Ainda em andamento.*aguardar/s);
    await p.call("w1", "perguntar", { pergunta: "Qual esquema?" });      // chega sem ninguém esperando
    const r2 = await terminalCall(p, mail, "t-1", "aguardar", {}, 10);
    expect(r2.text).toMatch(/^Pergunta da delegação d1/);
    const r3 = terminalCall(p, mail, "t-1", "responder", { delegacao: "d1", resposta: "Acme" }, 5000);
    await new Promise((r) => setTimeout(r, 0));
    await p.call("w1", "concluir", { status: "bloqueado", resumo: "sem acesso", evidencias: [] });
    expect((await r3).text).toMatch(/Resposta entregue[\s\S]*Resultado da delegação d1 \(Codex w1\): bloqueado/);
    expect((await terminalCall(p, mail, "t-1", "aguardar", {}, 10)).text).toBe("Nenhuma delegação em andamento.");
    expect((await terminalCall(p, mail, "c1", "aguardar", {}, 10)).ok).toBe(false);   // só no terminal
  });
});

describe("servidor do app e registro do terminal", () => {
  it("nomes diferentes: com o mesmo, a config do agente venceria e o worker ficaria sem concluir", () => {
    expect(PARTNER_SERVER).not.toBe(TERMINAL_SERVER);
    expect(partnerServerEntry("/x.js", { port: 1, token: "t" }, "s1", "worker").name).toBe(PARTNER_SERVER);
  });

  it("reconhece sessão do app pelo processo-ancestral, sem depender do ambiente", () => {
    const tree: Record<number, number> = { 900: 800, 800: 700, 700: 1 };   // mcp -> codex -> acp -> launchd
    const parentOf = (pid: number) => tree[pid] ?? null;
    expect(underApp(700, 900, parentOf)).toBe(true);
    expect(underApp(555, 900, parentOf)).toBe(false);
    expect(underApp(undefined, 900, parentOf)).toBe(false);
    expect(underApp(700, 900, () => null)).toBe(false);                    // ps falhou: não presume
    const loop = (pid: number) => pid;                                      // nunca sobe: para no limite
    expect(underApp(700, 900, loop)).toBe(false);
  });
});
