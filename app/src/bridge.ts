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
export const KEEPALIVE_MS = Number(process.env.AGENT_CONSOLE_KEEPALIVE_MS) || 30_000;

export interface Bridge {
  port: number;
  /** Token do receptor: o app o entrega ao servidor MCP das sessões que abre. */
  token: string;
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

/** Chamada de ferramenta do servidor MCP do console (partnerMcp.ts). */
export interface PartnerCall {
  key: string;
  tool: string;
  args: Record<string, unknown>;
  /** Só de sessões de terminal: a pasta do agente e quem é (claude, codex). */
  cwd?: string;
  from?: string;
}
export type PartnerHandler = (call: PartnerCall) => Promise<{ ok: boolean; text: string }>;
/** Controle pelo terminal (control.ts): responde na hora; um erro lançado volta como 400 com a mensagem. */
export type ControlHandler = (req: Record<string, unknown>) => unknown;

function parsePartnerCall(v: unknown): PartnerCall | null {
  const c = v as Partial<PartnerCall> | null;
  if (!c || typeof c !== "object" || typeof c.key !== "string" || !c.key || typeof c.tool !== "string" || !c.tool) return null;
  const args = c.args ?? {};
  if (typeof args !== "object" || Array.isArray(args)) return null;
  const opt = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  return { key: c.key, tool: c.tool, args: args as Record<string, unknown>, cwd: opt(c.cwd), from: opt(c.from) };
}

export async function startBridge(
  onEvent: (e: ExternalEvent) => void,
  home = CONSOLE_HOME,
  onPartner?: PartnerHandler,
  onControl?: ControlHandler,
): Promise<Bridge> {
  await fs.mkdir(home, { recursive: true, mode: 0o700 });
  await fs.chmod(home, 0o700);
  const tokenStr = randomBytes(32).toString("hex");
  const token = Buffer.from(tokenStr);

  const server = http.createServer((req, res) => {
    const done = (code: number) => { res.writeHead(code).end(); };
    const route = req.url === "/hook" ? "hook"
      : req.url === "/partner" && onPartner ? "partner"
      : req.url === "/control" && onControl ? "control" : null;
    if (req.method !== "POST" || !route) return done(404);
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
      if (route === "control") {
        let body: unknown;
        try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { body = null; }
        if (!body || typeof body !== "object" || Array.isArray(body)) return done(400);
        const json = (code: number, v: unknown) => res.writeHead(code, { "content-type": "application/json" }).end(JSON.stringify(v));
        try {
          return json(200, { ok: true, result: onControl!(body as Record<string, unknown>) });
        } catch (err) {
          return json(400, { ok: false, error: err instanceof Error ? err.message : String(err) });
        }
      }
      if (route === "partner") {
        let call: PartnerCall | null = null;
        try { call = parsePartnerCall(JSON.parse(Buffer.concat(chunks).toString("utf8"))); } catch { call = null; }
        if (!call) return done(400);
        // a chamada do terminal pode esperar minutos: cabeçalho já e um espaço de tempos em tempos,
        // senão o fetch do outro lado desiste (limites padrão de cabeçalho e de corpo do undici)
        res.writeHead(200, { "content-type": "application/json" });
        res.flushHeaders();
        const keepAlive = setInterval(() => res.write(" "), KEEPALIVE_MS);
        req.on("close", () => clearInterval(keepAlive));
        onPartner!(call)
          .catch((err) => ({ ok: false, text: err instanceof Error ? err.message : String(err) }))
          .then((r) => { clearInterval(keepAlive); res.end(JSON.stringify(r)); });
        return;
      }
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
    token: tokenStr,
    async close() {
      await new Promise<void>((resolve) => server.close(() => resolve()));
      try {
        const cur = JSON.parse(await fs.readFile(endpoint, "utf8")) as { token?: string };
        if (cur.token === tokenStr) await fs.unlink(endpoint);   // outro console pode ter assumido
      } catch { /* já não existe */ }
    },
  };
}
