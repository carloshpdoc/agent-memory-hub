/**
 * Parceiro de ponta a ponta, sem janela, com agentes de verdade:
 *   node dist/partnerSmoke.js <coordenador> <pasta> "<pedido>" [--allow] [--terminal]
 * Com --terminal o coordenador é o `claude -p` de verdade, fora do app, com o servidor MCP no
 * modo terminal (acha o receptor pelo endpoint.json e espera o retorno na mesma chamada).
 * Abre o coordenador com o servidor MCP do console; se ele delegar, abre o worker e imprime
 * as duas sessões. Termina quando o coordenador encerra o turno sem nada pendente.
 * Usa uma pasta temporária como home do receptor: não mexe no endpoint do app aberto.
 */
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { agentSpec } from "./agents";
import { AgentHost, type HostEvent } from "./agentHost";
import { startBridge } from "./bridge";
import { Mailbox, Partner, partnerServerEntry, routePartnerCall, TERMINAL_SERVER, type Role, type TerminalSessions } from "./partner";

const TIMEOUT_MS = 15 * 60_000;

async function main() {
  const argv = process.argv.slice(2);
  const terminal = argv.includes("--terminal");
  const [coordId, cwd, text, flag] = argv.filter((a) => a !== "--terminal");
  if (!coordId || !cwd || !text) {
    console.error('uso: partnerSmoke <claude|codex|cursor> <pasta> "<pedido>" [--allow]');
    process.exit(2);
  }
  const hosts = new Map<string, { host: AgentHost; label: string; cwd: string }>();
  let seq = 0;
  let partner: Partner;
  let finish: (code: number) => void = () => undefined;
  const done = new Promise<number>((r) => { finish = r; });

  const home = mkdtempSync(path.join(os.tmpdir(), "partner-smoke-"));
  const terminals: TerminalSessions = new Map();
  const mail = new Mailbox();
  const bridge = await startBridge(() => undefined, home,
    (c) => routePartnerCall(partner, mail, terminals, c, (id) => agentSpec(id).label));

  const prompt = (key: string, msg: string) => {
    if (terminals.has(key)) { console.log(`\n<<< [${key.slice(0, 10)}] retorno para o terminal (${msg.length} caracteres)`); mail.deliver(key, msg); return; }
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
        if (!terminal && !anyBusy && open.length === 0) finish(0);
      });
    } else if (e.type === "exit") {
      partner.onGone(key, "o processo do agente encerrou");
    } else if (e.type === "error") {
      console.log(`\n[${key}] erro: ${e.message}`);
    }
  }

  partner = new Partner({
    open: (agent, fromKey) => open(agent, "worker", terminals.get(fromKey)?.cwd ?? hosts.get(fromKey)!.cwd),
    label: (k) => terminals.get(k)?.label ?? hosts.get(k)?.label ?? k,
    cwd: (k) => terminals.get(k)?.cwd ?? hosts.get(k)!.cwd,
    busy: (k) => hosts.get(k)?.host.isBusy ?? false,
    prompt,
    notice: (k, t) => console.log(`\n[${k}] aviso: ${t}`),
  });

  const t0 = Date.now();
  const timer = setTimeout(() => { console.log("\nTEMPO ESGOTADO"); finish(1); }, TIMEOUT_MS);
  try {
    if (terminal) runTerminal(coordId, cwd, text, home, (code) => finish(code));
    else {
      const coord = await open(coordId, "coordenador", cwd);
      prompt(coord, text);
    }
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

/** O coordenador como no terminal do usuário: `claude -p`, servidor MCP registrado só nesta execução. */
function runTerminal(id: string, cwd: string, text: string, home: string, done: (code: number) => void) {
  if (id !== "claude") throw new Error("--terminal só com claude");
  const server = { type: "stdio", command: process.execPath, args: [path.join(__dirname, "partnerMcp.js"), "--from", "claude"] };
  const env: NodeJS.ProcessEnv = { ...process.env, AGENT_CONSOLE_HOME: home };
  delete env.AGENT_CONSOLE_SESSION;
  const child = spawn("claude", [
    "-p", text, "--mcp-config", JSON.stringify({ mcpServers: { [TERMINAL_SERVER]: server } }), "--strict-mcp-config",
    "--allowedTools", ["delegar", "responder", "aguardar"].map((t) => `mcp__${TERMINAL_SERVER}__${t}`).join(" ") + " Read",
  ], { cwd, env, stdio: ["ignore", "pipe", "inherit"] });
  console.log(`[terminal] claude -p aberto (pid ${child.pid})`);
  child.stdout.on("data", (b: Buffer) => process.stdout.write(`[terminal] ${b.toString()}`));
  child.on("exit", (code) => { console.log(`\n[terminal] claude saiu com ${code}`); done(code ?? 1); });
}

main();
