import { describe, expect, it } from "vitest";
import type { SessionUpdate } from "@agentclientprotocol/sdk";
import { addEntry, answerPermission, applyUpdate, emptyTimeline, type Timeline } from "../src/timeline";

const apply = (updates: SessionUpdate[], t: Timeline = emptyTimeline()) => updates.reduce(applyUpdate, t);
const text = (t: string) => ({ type: "text" as const, text: t });

describe("timeline", () => {
  it("junta pedaços seguidos da mesma fala e separa falas diferentes", () => {
    const t = apply([
      { sessionUpdate: "agent_message_chunk", content: text("Olá, ") },
      { sessionUpdate: "agent_message_chunk", content: text("vou ler o arquivo.") },
      { sessionUpdate: "agent_thought_chunk", content: text("pensando") },
      { sessionUpdate: "agent_message_chunk", content: text("Pronto.") },
    ]);
    expect(t.entries.map((e) => [e.kind, "text" in e ? e.text : ""])).toEqual([
      ["agent", "Olá, vou ler o arquivo."], ["thought", "pensando"], ["agent", "Pronto."],
    ]);
  });

  it("acompanha uma chamada de ferramenta do início ao fim, com diff", () => {
    const t = apply([
      { sessionUpdate: "tool_call", toolCallId: "c1", title: "Edit a.py", kind: "edit", status: "pending" },
      { sessionUpdate: "tool_call_update", toolCallId: "c1", status: "in_progress" },
      {
        sessionUpdate: "tool_call_update", toolCallId: "c1", status: "completed",
        content: [{ type: "diff", path: "/r/a.py", oldText: "x = 1", newText: "x = 2" }],
        locations: [{ path: "/r/a.py" }],
      },
    ]);
    expect(t.entries).toHaveLength(1);
    const e = t.entries[0];
    expect(e.kind === "tool" && [e.status, e.toolKind, e.diffs[0].newText, e.locations]).toEqual(
      ["completed", "edit", "x = 2", ["/r/a.py"]]);
  });

  it("update de chamada que não vimos nascer vira entrada, não some", () => {
    const t = apply([{ sessionUpdate: "tool_call_update", toolCallId: "zz", status: "failed", title: "Bash" }]);
    expect(t.entries[0]).toMatchObject({ kind: "tool", toolCallId: "zz", status: "failed", title: "Bash" });
  });

  it("plano é substituído inteiro e tipos desconhecidos são contados", () => {
    const t = apply([
      { sessionUpdate: "plan", entries: [{ content: "ler", priority: "high", status: "completed" }] },
      { sessionUpdate: "plan", entries: [{ content: "editar", priority: "high", status: "in_progress" }] },
      { sessionUpdate: "usage_update" } as unknown as SessionUpdate,
    ]);
    expect(t.plan.map((p) => p.content)).toEqual(["editar"]);
    expect(t.unknown).toEqual({ usage_update: 1 });
  });

  it("não muta o estado anterior (a tela compara por identidade)", () => {
    const t1 = apply([{ sessionUpdate: "agent_message_chunk", content: text("a") }]);
    const t2 = applyUpdate(t1, { sessionUpdate: "agent_message_chunk", content: text("b") });
    expect("text" in t1.entries[0] && t1.entries[0].text).toBe("a");
    expect(t2.entries[0]).not.toBe(t1.entries[0]);
  });

  it("registra a resposta a um pedido de permissão", () => {
    let t = addEntry(emptyTimeline(), { kind: "permission", title: "rm -rf build", options: ["Permitir", "Recusar"] });
    t = answerPermission(t, t.entries[0].id, "Recusar");
    expect(t.entries[0]).toMatchObject({ kind: "permission", answer: "Recusar" });
  });
});
