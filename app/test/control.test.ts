/** Controle pelo terminal: a mesma visão da tela, e só responde pedidos que estão pendentes. */
import { promises as fs, mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { startBridge, TOKEN_HEADER, type Bridge } from "../src/bridge";
import { Control, ControlError, SHOW_DEFAULT } from "../src/control";

const OPTIONS = [
  { optionId: "ok", name: "Permitir", kind: "allow_once" as const },
  { optionId: "no", name: "Negar", kind: "reject_once" as const },
];

function setup() {
  const answered: [string, string, string | null][] = [];
  const c = new Control((key, requestId, optionId) => answered.push([key, requestId, optionId]));
  c.track("s1", { label: "Codex · d1", agentId: "codex", cwd: "/r", branch: "fix/a" });
  c.apply("s1", { type: "ready", sessionId: "X", modes: null });
  c.sent("s1", "rode os testes");
  c.apply("s1", { type: "update", update: { sessionUpdate: "tool_call", toolCallId: "t1", title: "npm test", status: "in_progress" } });
  c.apply("s1", { type: "permission", requestId: "p1", title: "npm test", options: OPTIONS });
  return { c, answered };
}

describe("controle pelo terminal", () => {
  it("lista com status e pendências, e mostra as últimas entradas como texto", () => {
    const { c } = setup();
    expect(c.list()).toEqual([{ key: "s1", label: "Codex · d1", agentId: "codex", status: "aguardando você", cwd: "/r", branch: "fix/a", pending: 1 }]);
    const shown = c.show("s1", undefined);
    expect(shown.total).toBe(3);
    expect(shown.entries).toEqual(["> você\nrode os testes", "$ npm test [in_progress]", "? permissão: npm test (pendente)"]);
    expect(c.show("s1", 1).entries).toHaveLength(1);
    expect(SHOW_DEFAULT).toBeGreaterThan(3);
  });

  it("responde só pedido pendente com opção oferecida; o fechamento volta pelos eventos", () => {
    const { c, answered } = setup();
    expect(c.permissions()).toEqual([{ key: "s1", requestId: "p1", label: "Codex · d1", cwd: "/r", title: "npm test",
      options: OPTIONS }]);
    expect(() => c.respond("s1", "p1", "allow_always")).toThrow(ControlError);
    expect(() => c.respond("s1", "p9", "ok")).toThrow(/não está pendente/);
    expect(() => c.respond("s2", "p1", "ok")).toThrow(/sessão não encontrada/);
    expect(answered).toEqual([]);
    c.respond("s1", "p1", "ok");
    expect(answered).toEqual([["s1", "p1", "ok"]]);
    c.apply("s1", { type: "permission_closed", requestId: "p1", outcome: "ok" });
    expect(c.permissions()).toEqual([]);
    expect(c.list()[0].status).toBe("trabalhando");
    expect(c.show("s1", 1).entries).toEqual(["? permissão: npm test -> Permitir"]);
  });

  it("comando desconhecido e sessão fechada", () => {
    const { c } = setup();
    expect(() => c.handle({ cmd: "x" })).toThrow(ControlError);
    c.drop("s1");
    expect(c.handle({ cmd: "list" })).toEqual([]);
  });
});

let bridge: Bridge | null = null;
afterEach(async () => { await bridge?.close(); bridge = null; });

it("rota /control: só com token; erro de uso volta 400 com a mensagem", async () => {
  const home = mkdtempSync(path.join(os.tmpdir(), "console-"));
  const { c } = setup();
  bridge = await startBridge(() => undefined, home, undefined, (req) => c.handle(req));
  const { token } = JSON.parse(await fs.readFile(path.join(home, "endpoint.json"), "utf8"));
  const post = (headers: Record<string, string>, body: unknown) =>
    fetch(`http://127.0.0.1:${bridge!.port}/control`, { method: "POST", headers, body: JSON.stringify(body) });
  expect((await post({}, { cmd: "list" })).status).toBe(403);
  expect((await post({ [TOKEN_HEADER]: token }, [1])).status).toBe(400);
  const bad = await post({ [TOKEN_HEADER]: token }, { cmd: "answer", key: "s1", requestId: "p1", optionId: "zz" });
  expect([bad.status, (await bad.json()).error]).toEqual([400, "opção zz não foi oferecida nesse pedido"]);
  const ok = await post({ [TOKEN_HEADER]: token }, { cmd: "list" });
  expect((await ok.json()).result[0].pending).toBe(1);
});
