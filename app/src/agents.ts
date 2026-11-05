/**
 * Os agentes que o console sabe abrir. Cada um é um servidor ACP falando por stdio:
 * Claude e Codex pelos adaptadores oficiais (versões fixas no package.json), Cursor nativo.
 * Nenhum recebe flag de bypass: as permissões chegam ao app como session/request_permission.
 */
import { createRequire } from "node:module";
import path from "node:path";

export type AgentId = "claude" | "codex" | "cursor";

export interface LaunchSpec {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
}

export interface AgentSpec {
  id: AgentId;
  label: string;
  launch(): LaunchSpec;
}

export const AGENT_IDS: readonly AgentId[] = ["claude", "codex", "cursor"];

const requireFromHere = createRequire(__filename);

/** Caminho do script do adaptador instalado em node_modules (sem depender do PATH). */
export function adapterEntry(pkg: string): string {
  const manifest = requireFromHere.resolve(`${pkg}/package.json`);
  const bin = (requireFromHere(manifest) as { bin?: Record<string, string> }).bin ?? {};
  const rel = Object.values(bin)[0];
  if (!rel) throw new Error(`${pkg} não declara um executável`);
  return path.join(path.dirname(manifest), rel);
}

/** Marca o agente como aberto pelo console: a ponte de hooks ignora a sessão (o app já a vê pelo ACP). */
const consoleEnv = (extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv =>
  ({ ...process.env, AGENT_CONSOLE_SESSION: "1", ...extra });

/** Roda um script Node com o próprio binário do Electron (ELECTRON_RUN_AS_NODE): não exige node no PATH. */
function nodeScript(script: string): LaunchSpec {
  return {
    command: process.execPath,
    args: [script],
    env: consoleEnv({ ELECTRON_RUN_AS_NODE: "1" }),
  };
}

export const AGENTS: Record<AgentId, AgentSpec> = {
  claude: {
    id: "claude",
    label: "Claude Code",
    launch: () => nodeScript(adapterEntry("@agentclientprotocol/claude-agent-acp")),
  },
  codex: {
    id: "codex",
    label: "Codex",
    launch: () => nodeScript(adapterEntry("@agentclientprotocol/codex-acp")),
  },
  cursor: {
    id: "cursor",
    label: "Cursor",
    launch: () => ({ command: process.env.CURSOR_AGENT_PATH || "cursor-agent", args: ["acp"], env: consoleEnv() }),
  },
};

export function agentSpec(id: string): AgentSpec {
  if (!(AGENT_IDS as readonly string[]).includes(id)) throw new Error(`agente desconhecido: ${id}`);
  return AGENTS[id as AgentId];
}
