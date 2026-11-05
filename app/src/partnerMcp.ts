/**
 * Servidor MCP (stdio) do console. O agente o roda como processo filho; cada chamada de
 * ferramenta é repassada ao app por HTTP local com o token, e a resposta volta como resultado.
 *
 * Dois modos:
 * - sessão aberta pelo app: config pelo ambiente que o app põe em session/new (abaixo);
 * - terminal (registrado no Claude/Codex do usuário, `--from claude|codex`): acha o app pelo
 *   ~/.agent-console/endpoint.json a cada chamada, e delegar/responder esperam o retorno,
 *   porque o app não consegue mandar mensagem a uma sessão de terminal. Dentro de uma sessão
 *   do app (AGENT_CONSOLE_SESSION=1) esse registro não oferece nada: o do app já está lá.
 *
 * Config pelo ambiente (posto pelo app em session/new):
 *   AGENT_CONSOLE_PARTNER_URL   http://127.0.0.1:<porta>/partner
 *   AGENT_CONSOLE_PARTNER_TOKEN token do receptor
 *   AGENT_CONSOLE_PARTNER_KEY   chave da sessão no app
 *   AGENT_CONSOLE_PARTNER_ROLE  coordenador | worker
 */
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { PARTNER_SERVER, TERMINAL_PREFIX, toolsFor, WAIT_MS, type Role, type ToolDef } from "./partner";
import { CONSOLE_HOME, TOKEN_HEADER } from "./bridge";

export const PARTNER_TIMEOUT_MS = 60_000;   // abrir um agente pode levar alguns segundos

type Rpc = { jsonrpc: "2.0"; id?: number | string | null; method?: string; params?: Record<string, unknown> };

const env = process.env;
const fromApp = !!env.AGENT_CONSOLE_PARTNER_URL;
const fromIdx = process.argv.indexOf("--from");
const terminalFrom = fromIdx >= 0 ? process.argv[fromIdx + 1] : undefined;
const insideApp = !fromApp && env.AGENT_CONSOLE_SESSION === "1";
const role: Role = fromApp ? (env.AGENT_CONSOLE_PARTNER_ROLE === "worker" ? "worker" : "coordenador") : "terminal";
const tools: ToolDef[] = insideApp ? [] : toolsFor(role);
const terminalKey = `${TERMINAL_PREFIX}${randomUUID()}`;   // uma por sessão do agente (o processo vive com ela)

/** Onde mandar: o app que abriu a sessão, ou o app aberto agora (terminal). */
function target(): { url: string; token: string; key: string } | string {
  if (fromApp) {
    const { AGENT_CONSOLE_PARTNER_URL: url, AGENT_CONSOLE_PARTNER_TOKEN: token, AGENT_CONSOLE_PARTNER_KEY: key } = env;
    return url && token && key ? { url, token, key } : "servidor agent-console sem configuração do app";
  }
  const closed = "o Agent Console não está aberto: abra o app (a sessão delegada roda nele, com as permissões) e tente de novo";
  try {
    const ep = JSON.parse(readFileSync(path.join(CONSOLE_HOME, "endpoint.json"), "utf8")) as { port?: unknown; token?: unknown; pid?: unknown };
    if (typeof ep.port !== "number" || typeof ep.token !== "string" || typeof ep.pid !== "number") return closed;
    process.kill(ep.pid, 0);                                  // endpoint de um app que já fechou
    return { url: `http://127.0.0.1:${ep.port}/partner`, token: ep.token, key: terminalKey };
  } catch {
    return closed;
  }
}

function write(msg: unknown) {
  process.stdout.write(`${JSON.stringify(msg)}\n`);
}

async function forward(tool: string, args: unknown): Promise<{ ok: boolean; text: string }> {
  const t = target();
  if (typeof t === "string") return { ok: false, text: t };
  const extra = fromApp ? {} : { cwd: process.cwd(), from: terminalFrom };
  try {
    const res = await fetch(t.url, {
      method: "POST",
      headers: { "content-type": "application/json", [TOKEN_HEADER]: t.token },
      body: JSON.stringify({ key: t.key, tool, args: args ?? {}, ...extra }),
      // no terminal a chamada espera o retorno da delegação
      signal: AbortSignal.timeout(fromApp ? PARTNER_TIMEOUT_MS : WAIT_MS + PARTNER_TIMEOUT_MS),
    });
    if (!res.ok) return { ok: false, text: `o app recusou a chamada (HTTP ${res.status})` };
    const body = (await res.json()) as { ok?: unknown; text?: unknown };
    return { ok: body.ok === true, text: typeof body.text === "string" ? body.text : "resposta sem texto" };
  } catch (err) {
    return { ok: false, text: `o app não respondeu: ${err instanceof Error ? err.message : String(err)}` };
  }
}

async function handle(msg: Rpc): Promise<void> {
  if (msg.id === undefined || msg.id === null) return;            // notificação: nada a responder
  const reply = (result: unknown) => write({ jsonrpc: "2.0", id: msg.id, result });
  switch (msg.method) {
    case "initialize":
      return reply({
        protocolVersion: typeof msg.params?.protocolVersion === "string" ? msg.params.protocolVersion : "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: PARTNER_SERVER, version: "0.1.0" },
      });
    case "ping":
      return reply({});
    case "tools/list":
      return reply({ tools });
    case "tools/call": {
      const name = msg.params?.name;
      if (typeof name !== "string") return reply({ content: [{ type: "text", text: "chamada sem nome" }], isError: true });
      const r = await forward(name, msg.params?.arguments);
      return reply({ content: [{ type: "text", text: r.text }], isError: !r.ok });
    }
    default:
      write({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: `método não suportado: ${msg.method}` } });
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let msg: Rpc;
  try { msg = JSON.parse(line) as Rpc; } catch {
    write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "JSON inválido" } });
    return;
  }
  handle(msg).catch((err) => write({ jsonrpc: "2.0", id: msg.id ?? null, error: { code: -32603, message: String(err) } }));
});
rl.on("close", () => process.exit(0));
