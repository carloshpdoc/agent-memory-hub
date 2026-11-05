/**
 * Servidor MCP (stdio) que o console entrega a cada sessão que abre. O agente o roda como
 * processo filho; cada chamada de ferramenta é repassada ao app por HTTP local com o token
 * da sessão, e a resposta do app volta como resultado. Não guarda estado.
 *
 * Config pelo ambiente (posto pelo app em session/new):
 *   AGENT_CONSOLE_PARTNER_URL   http://127.0.0.1:<porta>/partner
 *   AGENT_CONSOLE_PARTNER_TOKEN token do receptor
 *   AGENT_CONSOLE_PARTNER_KEY   chave da sessão no app
 *   AGENT_CONSOLE_PARTNER_ROLE  coordenador | worker
 */
import readline from "node:readline";
import { PARTNER_SERVER, toolsFor, type Role } from "./partner";
import { TOKEN_HEADER } from "./bridge";

export const PARTNER_TIMEOUT_MS = 60_000;   // abrir um agente pode levar alguns segundos

type Rpc = { jsonrpc: "2.0"; id?: number | string | null; method?: string; params?: Record<string, unknown> };

const env = process.env;
const role: Role = env.AGENT_CONSOLE_PARTNER_ROLE === "worker" ? "worker" : "coordenador";

function write(msg: unknown) {
  process.stdout.write(`${JSON.stringify(msg)}\n`);
}

async function forward(tool: string, args: unknown): Promise<{ ok: boolean; text: string }> {
  const url = env.AGENT_CONSOLE_PARTNER_URL;
  const token = env.AGENT_CONSOLE_PARTNER_TOKEN;
  const key = env.AGENT_CONSOLE_PARTNER_KEY;
  if (!url || !token || !key) return { ok: false, text: "servidor agent-console sem configuração do app" };
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", [TOKEN_HEADER]: token },
      body: JSON.stringify({ key, tool, args: args ?? {} }),
      signal: AbortSignal.timeout(PARTNER_TIMEOUT_MS),
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
      return reply({ tools: toolsFor(role) });
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
