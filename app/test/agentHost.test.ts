/**
 * AgentHost contra um agente ACP falso, em memória, feito com o próprio SDK: o mesmo
 * protocolo que os adaptadores reais falam, sem processo nem rede.
 */
import { describe, expect, it } from "vitest";
import { agent, methods, PROTOCOL_VERSION, type PermissionOption } from "@agentclientprotocol/sdk";
import { AgentHost, type HostEvent } from "../src/agentHost";

const OPTIONS: PermissionOption[] = [
  { optionId: "ok", name: "Permitir", kind: "allow_once" },
  { optionId: "no", name: "Recusar", kind: "reject_once" },
];

/** Agente que, a cada prompt, fala, pede permissão para um comando e conta o resultado. */
function fakeAgent() {
  const seen: string[] = [];
  const app = agent({ name: "fake" })
    .onRequest(methods.agent.initialize, () => ({
      protocolVersion: PROTOCOL_VERSION, agentCapabilities: {}, agentInfo: { name: "fake", version: "1" },
    }))
    .onRequest(methods.agent.session.new, ({ params }) => {
      seen.push(`cwd:${params.cwd}`);
      return { sessionId: "S1" };
    })
    .onRequest(methods.agent.session.prompt, async ({ params, client, signal }) => {
      const text = params.prompt.map((b) => (b.type === "text" ? b.text : "")).join("");
      seen.push(`prompt:${text}`);
      await client.notify(methods.client.session.update, {
        sessionId: "S1", update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "vou rodar" } },
      });
      const perm = await client.request(methods.client.session.requestPermission, {
        sessionId: "S1", toolCall: { toolCallId: "t1", title: "rm -rf build" }, options: OPTIONS,
      });
      seen.push(`perm:${perm.outcome.outcome === "selected" ? perm.outcome.optionId : "cancelled"}`);
      if (signal.aborted || perm.outcome.outcome === "cancelled") return { stopReason: "cancelled" as const };
      return { stopReason: "end_turn" as const };
    });
  return { app, seen };
}

async function setup() {
  const events: HostEvent[] = [];
  const host = new AgentHost((e) => events.push(e));
  const fake = fakeAgent();
  const sessionId = await host.connect(fake.app, "/tmp/repo");
  return { host, events, fake, sessionId };
}

const until = async (cond: () => boolean) => {
  for (let i = 0; i < 200 && !cond(); i++) await new Promise((r) => setTimeout(r, 5));
  if (!cond()) throw new Error("condição não ocorreu");
};

describe("AgentHost", () => {
  it("inicializa e cria a sessão no cwd pedido", async () => {
    const { events, fake, sessionId } = await setup();
    expect(sessionId).toBe("S1");
    expect(fake.seen).toEqual(["cwd:/tmp/repo"]);
    expect(events[0]).toEqual({ type: "ready", sessionId: "S1", agentName: "fake" });
  });

  it("repassa as falas e espera o usuário decidir a permissão", async () => {
    const { host, events, fake } = await setup();
    const turn = host.prompt("limpa o build");
    await until(() => events.some((e) => e.type === "permission"));
    const ask = events.find((e) => e.type === "permission") as Extract<HostEvent, { type: "permission" }>;
    expect(ask.title).toBe("rm -rf build");
    expect(host.pendingPermissions).toEqual([ask.requestId]);
    expect(events.some((e) => e.type === "update")).toBe(true);
    host.answer(ask.requestId, "ok");
    expect(await turn).toBe("end_turn");
    expect(fake.seen).toContain("perm:ok");
    expect(events.at(-1)).toEqual({ type: "stop", reason: "end_turn" });
  });

  it("recusa opção que o agente não ofereceu", async () => {
    const { host, events } = await setup();
    void host.prompt("x");
    await until(() => events.some((e) => e.type === "permission"));
    const ask = events.find((e) => e.type === "permission") as Extract<HostEvent, { type: "permission" }>;
    expect(() => host.answer(ask.requestId, "sempre")).toThrow(/não foi oferecida/);
    host.answer(ask.requestId, "no");
  });

  it("cancelar o turno responde 'cancelled' à permissão pendente", async () => {
    const { host, events, fake } = await setup();
    const turn = host.prompt("x");
    await until(() => events.some((e) => e.type === "permission"));
    await host.cancel();
    expect(await turn).toBe("cancelled");
    expect(fake.seen).toContain("perm:cancelled");
    expect(events.some((e) => e.type === "permission_closed" && e.outcome === "cancelled")).toBe(true);
    expect(host.pendingPermissions).toEqual([]);
  });

  it("não aceita dois turnos ao mesmo tempo nem mensagem vazia", async () => {
    const { host, events } = await setup();
    const turn = host.prompt("um");
    await expect(host.prompt("dois")).rejects.toThrow(/turno anterior/);
    await until(() => events.some((e) => e.type === "permission"));
    host.answer((events.find((e) => e.type === "permission") as { requestId: string }).requestId, "ok");
    await turn;
    await expect(host.prompt("   ")).rejects.toThrow(/vazia/);
  });
});
