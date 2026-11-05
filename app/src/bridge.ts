/**
 * Receptor da ponte de hooks (hooks/console_bridge.py). Ao abrir, escuta só em 127.0.0.1
 * numa porta livre e grava {port, token, pid} em ~/.agent-console/endpoint.json (arquivo 600,
 * pasta 700). O hook só entrega com o token; sem o app no ar ele grava no spool, que é lido
 * aqui ao abrir. Ao fechar, remove o endpoint se ainda for o deste processo.
 */
import { randomBytes, timingSafeEqual } from "node:crypto";
import { promises as fs } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { parseExternal, parseSpool, type ExternalEvent } from "./external";

export const CONSOLE_HOME = process.env.AGENT_CONSOLE_HOME || path.join(os.homedir(), ".agent-console");
const MAX_BODY = 64 * 1024;
export const TOKEN_HEADER = "x-agent-console-token";

export interface Bridge {
  port: number;
  close(): Promise<void>;
}

export interface SpoolRead {
  events: ExternalEvent[];
  dropped: number;
  files: number;
}

/** Eventos do spool (rotação antiga primeiro), em ordem de chegada. */
export async function readSpool(home = CONSOLE_HOME): Promise<SpoolRead> {
  const out: SpoolRead = { events: [], dropped: 0, files: 0 };
  for (const name of ["spool.jsonl.1", "spool.jsonl"]) {
    let text: string;
    try { text = await fs.readFile(path.join(home, name), "utf8"); } catch { continue; }
    out.files++;
    const r = parseSpool(text);
    out.events.push(...r.events);
    out.dropped += r.dropped;
  }
  return out;
}

function sameToken(given: string | string[] | undefined, token: Buffer): boolean {
  if (typeof given !== "string") return false;
  const g = Buffer.from(given);
  return g.length === token.length && timingSafeEqual(g, token);
}

export async function startBridge(onEvent: (e: ExternalEvent) => void, home = CONSOLE_HOME): Promise<Bridge> {
  await fs.mkdir(home, { recursive: true, mode: 0o700 });
  await fs.chmod(home, 0o700);
  const tokenStr = randomBytes(32).toString("hex");
  const token = Buffer.from(tokenStr);

  const server = http.createServer((req, res) => {
    const done = (code: number) => { res.writeHead(code).end(); };
    if (req.method !== "POST" || req.url !== "/hook") return done(404);
    if (!sameToken(req.headers[TOKEN_HEADER], token)) return done(403);
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) { done(413); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      if (size > MAX_BODY) return;
      let ev: ExternalEvent | null = null;
      try { ev = parseExternal(JSON.parse(Buffer.concat(chunks).toString("utf8"))); } catch { ev = null; }
      if (!ev) return done(400);
      onEvent(ev);
      done(204);
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const port = (server.address() as { port: number }).port;

  const endpoint = path.join(home, "endpoint.json");
  const tmp = `${endpoint}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify({ port, token: tokenStr, pid: process.pid }), { mode: 0o600 });
  await fs.rename(tmp, endpoint);

  return {
    port,
    async close() {
      await new Promise<void>((resolve) => server.close(() => resolve()));
      try {
        const cur = JSON.parse(await fs.readFile(endpoint, "utf8")) as { token?: string };
        if (cur.token === tokenStr) await fs.unlink(endpoint);   // outro console pode ter assumido
      } catch { /* já não existe */ }
    },
  };
}
