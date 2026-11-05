import { describe, expect, it } from "vitest";
import { buildHandoff, NEXT_STEP, splitHandoff } from "../src/handoff";
import { addEntry, applyUpdate, emptyTimeline, type Timeline } from "../src/timeline";

const text = (t: string) => ({ type: "text" as const, text: t });

function session(): Timeline {
  let t = emptyTimeline();
  t = addEntry(t, { kind: "user", text: "Corrija o login que falha com senha vazia" });
  t = applyUpdate(t, { sessionUpdate: "agent_thought_chunk", content: text("pensando em segredo") });
  t = applyUpdate(t, { sessionUpdate: "agent_message_chunk", content: text("Vou ler o Login.swift.") });
  t = applyUpdate(t, { sessionUpdate: "tool_call", toolCallId: "r", title: "Read Login.swift", kind: "read", status: "completed", locations: [{ path: "/r/Login.swift" }] });
  t = applyUpdate(t, {
    sessionUpdate: "tool_call", toolCallId: "e", title: "Edit Login.swift", kind: "edit", status: "completed",
    content: [{ type: "diff", path: "/r/Login.swift", oldText: "a\nb", newText: "a\nb\nc" }], locations: [{ path: "/r/Login.swift" }],
  });
  t = applyUpdate(t, { sessionUpdate: "tool_call", toolCallId: "x", title: "swift test", kind: "execute", status: "failed" });
  t = applyUpdate(t, { sessionUpdate: "plan", entries: [{ content: "rodar testes", priority: "high", status: "in_progress" }] });
  t = addEntry(t, { kind: "stop", reason: "end_turn" });
  t = addEntry(t, { kind: "user", text: "Agora rode os testes de novo" });
  return t;
}

const base = { fromLabel: "Claude Code", toLabel: "Codex", cwd: "/r", branch: "console/login", sessionId: "abc-123" };

describe("pacote de continuação", () => {
  it("resumo no topo, últimos turnos no fim e ponteiro para o histórico", () => {
    const p = buildHandoff({ ...base, timeline: session() });
    expect(p).toContain("# Continuação de trabalho: Claude Code → Codex");
    expect(p).toContain("branch `console/login`");
    expect(p).toMatch(/## Pedido original\nCorrija o login/);
    expect(p).toMatch(/## Último pedido\nAgora rode os testes/);
    expect(p).toContain("- [in_progress] rodar testes");
    expect(p).toContain("- /r/Login.swift (2→3 linhas)");
    const novo = buildHandoff({ ...base, timeline: applyUpdate(emptyTimeline(), {
      sessionUpdate: "tool_call", toolCallId: "w", title: "Write n.txt", kind: "edit", status: "completed",
      content: [{ type: "diff", path: "/r/n.txt", newText: "maçã\npera\n" }],
    }) });
    expect(novo).toContain("- /r/n.txt (novo, 2 linhas)");             // quebra final não conta
    expect(p).not.toMatch(/## Arquivos lidos/);                 // lido e alterado: só em alterados
    expect(p).toContain("- swift test (failed)");
    expect(p).toMatch(/## Falhas\n- swift test/);
    expect(p).toContain("id `abc-123`");
    expect(p).not.toContain("pensando em segredo");             // pensamento não vai junto
    expect(p.indexOf("## Últimos turnos")).toBeLessThan(p.indexOf("### Usuário\nAgora rode"));
  });

  it("orçamento corta os turnos mais antigos, conta o que omitiu e mantém o mais recente", () => {
    let t = emptyTimeline();
    for (let i = 0; i < 40; i++) t = addEntry(t, { kind: "user", text: `pedido ${i} ` + "x".repeat(400) });
    const p = buildHandoff({ ...base, timeline: t }, 4000);
    expect(p.length).toBeLessThanOrEqual(4000 + 3100);           // o último turno entra mesmo que estoure
    expect(p).toMatch(/\(\d+ item\(ns\) anteriores omitidos/);
    expect(p).toContain("pedido 39");
    expect(p).not.toContain("### Usuário\npedido 1 ");
  });

  it("sessão vazia, sem id e interrompida ainda dá um pacote válido", () => {
    const p = buildHandoff({ fromLabel: "Codex", toLabel: "Cursor", cwd: "/r", timeline: emptyTimeline(), interrupted: true });
    expect(p).toContain("foi interrompido no meio");
    expect(p).not.toContain("## Histórico completo");
    expect(p).not.toContain("## Pedido original");
  });

  it("separa o pacote enviado do próximo passo", () => {
    const pack = buildHandoff({ ...base, timeline: session() });
    expect(splitHandoff(`${pack}\n\n${NEXT_STEP}\nrode os testes\n`)).toEqual({ pack, next: "rode os testes" });
    expect(splitHandoff(pack)).toEqual({ pack, next: "" });
    expect(splitHandoff("oi\n## Próximo passo\nx")).toBeNull();     // mensagem comum não vira pacote
  });
});
