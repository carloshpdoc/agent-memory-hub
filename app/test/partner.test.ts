import { describe, expect, it } from "vitest";
import { MAX_ACTIVE, MAX_NUDGES, Partner, toolsFor, type PartnerDeps } from "../src/partner";

/** Sessões de mentira: registra o que o Partner manda e quem está ocupado. */
function harness() {
  const prompts: { key: string; text: string }[] = [];
  const notices: { key: string; text: string }[] = [];
  const busy = new Set<string>();
  let n = 0;
  const deps: PartnerDeps = {
    open: async (agent) => { if (agent === "quebrado") throw new Error("não abriu quebrado"); return `w${++n}`; },
    label: (k) => (k.startsWith("w") ? `Codex ${k}` : `Claude ${k}`),
    cwd: () => "/r",
    busy: (k) => busy.has(k),
    prompt: (key, text) => prompts.push({ key, text }),
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
});
