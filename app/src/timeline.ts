/**
 * Linha do tempo de uma sessão, montada a partir dos session/update do ACP. Função pura
 * (estado, evento) -> estado: o renderer só desenha o que sai daqui, e os testes cobrem a
 * montagem sem processo nem janela.
 */
import type { SessionUpdate, ToolCallContent, PlanEntry, StopReason } from "@agentclientprotocol/sdk";

export type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; text: string }
  | { kind: "thought"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      toolCallId: string;
      title: string;
      status: string;
      toolKind?: string;
      diffs: { path: string; oldText?: string | null; newText: string }[];
      output: string;
      locations: string[];
    }
  | { kind: "permission"; id: string; title: string; options: string[]; answer?: string }
  | { kind: "stop"; id: string; reason: StopReason | "error"; detail?: string }
  | { kind: "notice"; id: string; text: string };

export interface Timeline {
  entries: Entry[];
  plan: PlanEntry[];
  mode?: string;
  /** Updates de tipos que esta versão não mostra: contados, nunca descartados em silêncio. */
  unknown: Record<string, number>;
  seq: number;
}

export const emptyTimeline = (): Timeline => ({ entries: [], plan: [], unknown: {}, seq: 0 });

function nextId(t: Timeline): [string, number] {
  return [`e${t.seq + 1}`, t.seq + 1];
}

function textOf(content: unknown): string {
  const c = content as { type?: string; text?: string; uri?: string } | undefined;
  if (!c) return "";
  if (c.type === "text") return c.text ?? "";
  if (c.type === "resource_link") return `[${c.uri ?? "recurso"}]`;
  return c.type ? `[${c.type}]` : "";
}

/** Junta pedaços (chunks) no último bloco do mesmo tipo; senão abre um bloco novo. */
function appendChunk(t: Timeline, kind: "user" | "agent" | "thought", text: string): Timeline {
  const last = t.entries[t.entries.length - 1];
  if (last && last.kind === kind) {
    const merged = { ...last, text: last.text + text };
    return { ...t, entries: [...t.entries.slice(0, -1), merged] };
  }
  const [id, seq] = nextId(t);
  return { ...t, seq, entries: [...t.entries, { kind, id, text }] };
}

function applyToolContent(content: ToolCallContent[] | null | undefined) {
  const diffs: { path: string; oldText?: string | null; newText: string }[] = [];
  let output = "";
  for (const c of content ?? []) {
    if (c.type === "diff") diffs.push({ path: c.path, oldText: c.oldText, newText: c.newText });
    else if (c.type === "content") output += textOf(c.content);
    else if (c.type === "terminal") output += `[terminal ${c.terminalId}]`;
  }
  return { diffs, output };
}

export function applyUpdate(t: Timeline, u: SessionUpdate): Timeline {
  switch (u.sessionUpdate) {
    case "user_message_chunk":
      return appendChunk(t, "user", textOf(u.content));
    case "agent_message_chunk":
      return appendChunk(t, "agent", textOf(u.content));
    case "agent_thought_chunk":
      return appendChunk(t, "thought", textOf(u.content));
    case "tool_call": {
      const { diffs, output } = applyToolContent(u.content);
      const [id, seq] = nextId(t);
      return {
        ...t,
        seq,
        entries: [
          ...t.entries,
          {
            kind: "tool", id, toolCallId: u.toolCallId, title: u.title, status: u.status ?? "pending",
            toolKind: u.kind ?? undefined, diffs, output, locations: (u.locations ?? []).map((l) => l.path),
          },
        ],
      };
    }
    case "tool_call_update": {
      const idx = t.entries.findIndex((e) => e.kind === "tool" && e.toolCallId === u.toolCallId);
      if (idx < 0) {
        // update de uma chamada que não vimos nascer: vira entrada própria em vez de sumir
        return applyUpdate(t, { ...u, sessionUpdate: "tool_call", title: u.title ?? u.toolCallId } as SessionUpdate);
      }
      const cur = t.entries[idx] as Extract<Entry, { kind: "tool" }>;
      const extra = u.content ? applyToolContent(u.content) : null;
      const next: Entry = {
        ...cur,
        title: u.title ?? cur.title,
        status: u.status ?? cur.status,
        toolKind: u.kind ?? cur.toolKind,
        diffs: extra ? extra.diffs : cur.diffs,
        output: extra ? extra.output : cur.output,
        locations: u.locations ? u.locations.map((l) => l.path) : cur.locations,
      };
      const entries = t.entries.slice();
      entries[idx] = next;
      return { ...t, entries };
    }
    case "plan":
      return { ...t, plan: u.entries };
    case "current_mode_update":
      return { ...t, mode: u.currentModeId };
    default: {
      const k = (u as { sessionUpdate: string }).sessionUpdate;
      return { ...t, unknown: { ...t.unknown, [k]: (t.unknown[k] ?? 0) + 1 } };
    }
  }
}

/** Omit que respeita a união (Omit<União> perderia os campos de cada variante). */
export type NewEntry = Entry extends infer E ? (E extends Entry ? Omit<E, "id"> : never) : never;

export function addEntry(t: Timeline, entry: NewEntry): Timeline {
  const [id, seq] = nextId(t);
  return { ...t, seq, entries: [...t.entries, { ...entry, id } as Entry] };
}

export function answerPermission(t: Timeline, entryId: string, answer: string): Timeline {
  return {
    ...t,
    entries: t.entries.map((e) => (e.kind === "permission" && e.id === entryId ? { ...e, answer } : e)),
  };
}
