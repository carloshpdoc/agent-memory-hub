/**
 * Uma worktree git por tarefa, para dois agentes nunca editarem os mesmos arquivos.
 * Fica em ~/agent-console/worktrees/<repo>/<nome>, no branch console/<nome>, a partir do
 * HEAD do repo. Nada é apagado aqui: remover worktree é decisão do usuário.
 */
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
export const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,47}$/;

export function worktreeRoot(): string {
  return process.env.AGENT_CONSOLE_WORKTREES || path.join(os.homedir(), "agent-console", "worktrees");
}

async function git(cwd: string, ...args: string[]): Promise<string> {
  const { stdout } = await run("git", ["-C", cwd, ...args], { timeout: 30_000 });
  return stdout.trim();
}

export async function repoTop(dir: string): Promise<string> {
  try {
    return await git(dir, "rev-parse", "--show-toplevel");
  } catch {
    throw new Error(`não é um repositório git: ${dir}`);
  }
}

export interface Worktree {
  path: string;
  branch: string;
  base: string;
}

export async function createWorktree(repoDir: string, name: string): Promise<Worktree> {
  if (!NAME_RE.test(name)) throw new Error(`nome inválido para a tarefa: ${JSON.stringify(name)} (use a-z, 0-9, . _ -)`);
  const top = await repoTop(repoDir);
  const dest = path.join(worktreeRoot(), path.basename(top), name);
  if (existsSync(dest)) throw new Error(`já existe: ${dest}`);
  const branch = `console/${name}`;
  const base = await git(top, "rev-parse", "--short", "HEAD");
  await git(top, "worktree", "add", "-b", branch, dest, "HEAD");
  return { path: dest, branch, base };
}
