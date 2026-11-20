/**
 * Histórico do hub (aba Histórico): sessões das duas máquinas e das três ferramentas, lidas
 * por scripts/app_history.py. O app não fala com o banco nem guarda credencial: só lê o JSON
 * que o script imprime. Aqui fica a parte pura (validação, filtros), que a tela também usa;
 * a execução fica em historyRunner.ts, só no processo principal.
 */

export interface HubSession {
  sessionId: string;
  startedAt: string;
  machine: string;
  tool: string;
  project: string;
  summary: string;
}

export interface HubTurn { role: "user" | "agent"; text: string }
export interface HubDetail extends HubSession { turns: HubTurn[] }

export interface HistoryFilter { machine?: string; tool?: string; project?: string; text?: string }

const TOOL_LABELS: Record<string, string> = { "claude-code": "Claude Code", codex: "Codex", cursor: "Cursor" };
export const toolLabel = (tool: string) => TOOL_LABELS[tool] ?? tool;

/** Valor ausente no hub aparece assim na tela e nos filtros (nunca some da contagem). */
export const NONE = "(sem)";

const SID_RE = /^[A-Za-z0-9_-]{8,128}$/;
export const validSessionId = (v: unknown): v is string => typeof v === "string" && SID_RE.test(v);

const text = (v: unknown) => (typeof v === "string" && v.trim() ? v : NONE);

function asObject(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("resposta do hub fora do formato");
  const o = raw as Record<string, unknown>;
  if (typeof o.error === "string") throw new Error(o.error);
  return o;
}

function toSession(r: Record<string, unknown>): HubSession | null {
  if (!validSessionId(r.session_id)) return null;
  return {
    sessionId: r.session_id, startedAt: typeof r.started_at === "string" ? r.started_at : "",
    machine: text(r.machine), tool: text(r.tool), project: text(r.project),
    summary: typeof r.summary === "string" ? r.summary : "",
  };
}

/** Linhas sem id válido são contadas em `dropped`, não descartadas em silêncio. */
export function parseList(raw: unknown): { sessions: HubSession[]; dropped: number } {
  const o = asObject(raw);
  if (!Array.isArray(o.sessions)) throw new Error("resposta do hub sem lista de sessões");
  const sessions: HubSession[] = [];
  let dropped = 0;
  for (const r of o.sessions) {
    const s = r && typeof r === "object" ? toSession(r as Record<string, unknown>) : null;
    if (s) sessions.push(s); else dropped++;
  }
  return { sessions, dropped };
}

export function parseDetail(raw: unknown): HubDetail {
  const o = asObject(raw);
  const s = toSession(o);
  if (!s || !Array.isArray(o.turns)) throw new Error("sessão do hub fora do formato");
  const turns = o.turns.flatMap((t): HubTurn[] => {
    const r = t as Record<string, unknown>;
    return r && (r.role === "user" || r.role === "agent") && typeof r.text === "string" ? [{ role: r.role, text: r.text }] : [];
  });
  return { ...s, turns };
}

export function parsePack(raw: unknown): string {
  const o = asObject(raw);
  if (typeof o.pack !== "string" || !o.pack) throw new Error("pacote de continuação vazio");
  return o.pack;
}

/** Valores distintos de cada filtro, em ordem alfabética. */
export function facets(list: HubSession[]) {
  const uniq = (k: "machine" | "tool" | "project") => [...new Set(list.map((s) => s[k]))].sort((a, b) => a.localeCompare(b));
  return { machines: uniq("machine"), tools: uniq("tool"), projects: uniq("project") };
}

export function filterSessions(list: HubSession[], f: HistoryFilter): HubSession[] {
  const q = f.text?.trim().toLowerCase();
  return list.filter((s) =>
    (!f.machine || s.machine === f.machine) && (!f.tool || s.tool === f.tool) && (!f.project || s.project === f.project) &&
    (!q || `${s.summary} ${s.project} ${s.sessionId}`.toLowerCase().includes(q)));
}
