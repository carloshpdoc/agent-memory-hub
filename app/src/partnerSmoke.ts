/**
 * Parceiro de ponta a ponta, sem janela, com agentes de verdade:
 *   node dist/partnerSmoke.js <coordenador> <pasta> "<pedido>" [--allow]
 * Abre o coordenador com o servidor MCP do console; se ele delegar, abre o worker e imprime
 * as duas sessões. Termina quando o coordenador encerra o turno sem nada pendente.
 * Usa uma pasta temporária como home do receptor: não mexe no endpoint do app aberto.
 */
import { mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { agentSpec } from "./agents";
import { AgentHost, type HostEvent } from "./agentHost";
import { startBridge } from "./bridge";
import { Partner, partnerServerEntry, type Role } from "./partner";

const TIMEOUT_MS = 15 * 60_000;

async function main() {
  const [coordId, cwd, text, flag] = process.argv.slice(2);
  if (!coordId || !cwd || !text) {
    console.error('uso: partnerSmoke <claude|codex|cursor> <pasta> "<pedido>" [--allow]');
    process.exit(2);
  }
  const hosts = new Map<string, { host: AgentHost; label: string; cwd: string }>();
  let seq = 0;
  let partner: Partner;
  let finish: (code: number) => void = () => undefined;
  const done = new Promise<number>((r) => { finish = r; });

  const bridge = await startBridge(() => undefined, mkdtempSync(path.join(os.tmpdir(), "partner-smoke-")),
    (c) => partner.call(c.key, c.tool, c.args));

  const prompt = (key: string, msg: string) => {
    console.log(`\n>>> [${key}] mensagem do parceiro:\n${msg.split("\n").map((l) => `    ${l}`).join("\n")}`);
    hosts.get(key)!.host.prompt(msg).catch(() => undefined);
  };

  async function open(agentId: string, role: Role, dir: string): Promise<string> {
    const spec = agentSpec(agentId);
    const key = `${role === "worker" ? "w" : "c"}${++seq}`;
    const host = new AgentHost((e) => onEvent(key, e));
    hosts.set(key, { host, label: `${spec.label} ${key}`, cwd: dir });
    await host.launch(spec.launch(), dir, [partnerServerEntry(path.join(__dirname, "partnerMcp.js"), bridge, key, role)]);
    await host.useAskingMode();
    console.log(`[${key}] aberto: ${spec.label} (${role})`);
    return key;
  }

  function onEvent(key: string, e: HostEvent) {
    const h = hosts.get(key);
    if (e.type === "update") {
      const u = e.update as { sessionUpdate: string; content?: { text?: string }; title?: string; status?: string; toolCallId?: string };
      if (u.sessionUpdate === "agent_message_chunk") process.stdout.write(u.content?.text ?? "");
      else if (u.sessionUpdate === "tool_call" || (u.sessionUpdate === "tool_call_update" && u.status)) {
        console.log(`\n[${key}] ${u.sessionUpdate} ${u.title ?? u.toolCallId} ${u.status ?? ""}`);
      }
    } else if (e.type === "permission") {
      const pick = flag === "--allow" ? e.options.find((o) => o.kind === "allow_once") : e.options.find((o) => o.kind.startsWith("reject"));
      console.log(`\n[${key}] permissão: ${e.title} -> ${pick ? pick.name : "cancelar"}`);
      h?.host.answer(e.requestId, pick ? pick.optionId : null);
    } else if (e.type === "stop") {
      console.log(`\n[${key}] fim do turno: ${e.reason}`);
      partner.onStop(key, e.reason);
      setImmediate(() => {
        const open = partner.list().filter((d) => d.state === "trabalhando" || d.state === "perguntou");
        const anyBusy = [...hosts.values()].some((x) => x.host.isBusy);
        if (!anyBusy && open.length === 0) finish(0);
      });
    } else if (e.type === "exit") {
      partner.onGone(key, "o processo do agente encerrou");
    } else if (e.type === "error") {
      console.log(`\n[${key}] erro: ${e.message}`);
    }
  }

  partner = new Partner({
    open: (agent, fromKey) => open(agent, "worker", hosts.get(fromKey)!.cwd),
    label: (k) => hosts.get(k)?.label ?? k,
    cwd: (k) => hosts.get(k)!.cwd,
    busy: (k) => hosts.get(k)?.host.isBusy ?? false,
    prompt,
    notice: (k, t) => console.log(`\n[${k}] aviso: ${t}`),
  });

  const t0 = Date.now();
  const timer = setTimeout(() => { console.log("\nTEMPO ESGOTADO"); finish(1); }, TIMEOUT_MS);
  try {
    const coord = await open(coordId, "coordenador", cwd);
    prompt(coord, text);
  } catch (err) {
    console.error(`FALHOU: ${err instanceof Error ? err.message : String(err)}`);
    finish(1);
  }
  const code = await done;
  clearTimeout(timer);
  console.log(`\n--- ${((Date.now() - t0) / 1000).toFixed(1)}s; delegações: ${JSON.stringify(partner.list().map((d) => ({ id: d.id, agent: d.agent, state: d.state, nudges: d.nudges })))}`);
  for (const { host } of hosts.values()) host.close();
  await bridge.close();
  process.exit(code);
}

main();
