/**
 * Sessões abertas fora do console (Claude/Codex no terminal comum), vistas pelos eventos que
 * hooks/console_bridge.py entrega ou deixa no spool. Puro: validação e agregação.
 */
export interface ExternalEvent {
  v: 1;
  tool: "claude" | "codex";
  event: string;
  session_id: string;
  cwd: string;
  ts: number;
  tool_name?: string;
  detail?: string;
}

export interface ExternalSession {
  key: string;            // tool:session_id
  tool: ExternalEvent["tool"];
  sessionId: string;
  cwd: string;
  firstTs: number;
  lastTs: number;
  lastEvent: string;
  lastDetail?: string;
  events: number;
  ended: boolean;
}

const TOOLS = new Set(["claude", "codex"]);
const MAX_STR = 4096;
const END_EVENTS = new Set(["SessionEnd"]);

const okStr = (v: unknown, required = true) =>
  v === undefined ? !required : typeof v === "string" && v.length <= MAX_STR;

/** O evento como chegou (JSON já lido) ou null se não tiver o formato da ponte. */
export function parseExternal(raw: unknown): ExternalEvent | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const e = raw as Record<string, unknown>;
  if (e.v !== 1 || !TOOLS.has(e.tool as string)) return null;
  if (!okStr(e.event) || !okStr(e.session_id) || !okStr(e.cwd)) return null;
  if (typeof e.ts !== "number" || !Number.isFinite(e.ts)) return null;
  if (!okStr(e.tool_name, false) || !okStr(e.detail, false)) return null;
  if (!e.session_id) return null;
  const out: ExternalEvent = {
    v: 1, tool: e.tool as ExternalEvent["tool"], event: e.event as string,
    session_id: e.session_id as string, cwd: e.cwd as string, ts: e.ts,
  };
  if (e.tool_name) out.tool_name = e.tool_name as string;
  if (e.detail) out.detail = e.detail as string;
  return out;
}

/** Linhas do spool: eventos válidos e quantas foram descartadas (nunca some sem contar). */
export function parseSpool(text: string): { events: ExternalEvent[]; dropped: number } {
  const events: ExternalEvent[] = [];
  let dropped = 0;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let ev: ExternalEvent | null = null;
    try { ev = parseExternal(JSON.parse(line)); } catch { ev = null; }
    if (ev) events.push(ev); else dropped++;
  }
  return { events, dropped };
}

/** Agrega por sessão. Imutável: devolve um Map novo. Evento antigo não sobrescreve o recente. */
export function foldExternal(prev: Map<string, ExternalSession>, ev: ExternalEvent): Map<string, ExternalSession> {
  const key = `${ev.tool}:${ev.session_id}`;
  const cur = prev.get(key);
  const next = new Map(prev);
  if (!cur) {
    next.set(key, {
      key, tool: ev.tool, sessionId: ev.session_id, cwd: ev.cwd, firstTs: ev.ts, lastTs: ev.ts,
      lastEvent: ev.event, lastDetail: ev.detail, events: 1, ended: END_EVENTS.has(ev.event),
    });
    return next;
  }
  const newer = ev.ts >= cur.lastTs;
  next.set(key, {
    ...cur,
    cwd: cur.cwd || ev.cwd,
    firstTs: Math.min(cur.firstTs, ev.ts),
    lastTs: newer ? ev.ts : cur.lastTs,
    lastEvent: newer ? ev.event : cur.lastEvent,
    lastDetail: newer ? ev.detail : cur.lastDetail,
    events: cur.events + 1,
    ended: newer ? END_EVENTS.has(ev.event) : cur.ended,
  });
  return next;
}
