/**
 * Controle do app pelo terminal (`amh app`, scripts/app_control.py), pela rota /control da
 * ponte. Mantém, no processo principal, a mesma visão de cada sessão que a tela monta
 * (sessionView.ts) e responde a: listar sessões, ver as últimas entradas, listar e
 * responder pedidos de permissão. Quem responde é o usuário, digitando no terminal: nada
 * aqui aprova sozinho.
 */
import type { HostEvent } from "./agentHost";
import { applyEvent, newView, userSent, type SessionView } from "./sessionView";
import type { Entry } from "./timeline";

export const SHOW_DEFAULT = 15;
export const SHOW_MAX = 200;
const ENTRY_CLIP = 2000;

export interface SessionMeta { label: string; agentId: string; cwd: string; branch?: string }
type Tracked = SessionView & SessionMeta;

export type Answer = (key: string, requestId: string, optionId: string | null) => void;

export class ControlError extends Error {}

const clip = (t: string, n = ENTRY_CLIP) => (t.length <= n ? t : `${t.slice(0, n)}… [+${t.length - n}]`);

/** Uma entrada da linha do tempo como texto de terminal. */
export function entryText(e: Entry): string {
  switch (e.kind) {
    case "user": return `> você\n${clip(e.text)}`;
    case "agent": return `< agente\n${clip(e.text)}`;
    case "thought": return `~ pensando: ${clip(e.text, 300)}`;
    case "tool": {
      const files = e.diffs.length ? `\n  arquivos: ${e.diffs.map((d) => d.path).join(", ")}` : "";
      return `$ ${e.title} [${e.status}]${files}${e.output.trim() ? `\n${clip(e.output.trim())}` : ""}`;
    }
    case "permission": return `? permissão: ${e.title}${e.answer ? ` -> ${e.answer}` : " (pendente)"}`;
    case "stop": return `-- fim do turno (${e.reason})`;
    case "notice": return `* ${e.text}`;
  }
}

export class Control {
  private views = new Map<string, Tracked>();

  constructor(private readonly answer: Answer) {}

  /** Os modos chegam no evento ready. */
  track(key: string, meta: SessionMeta) {
    if (!this.views.has(key)) this.views.set(key, { ...newView(null), ...meta });
  }

  /** Mensagem do usuário enviada pela tela (ela não passa pelos eventos do agente). */
  sent(key: string, text: string) {
    const v = this.views.get(key);
    if (v) userSent(v, text);
  }

  apply(key: string, ev: HostEvent) {
    const v = this.views.get(key);
    if (v) applyEvent(v, ev);
  }

  drop(key: string) {
    this.views.delete(key);
  }

  private view(key: unknown): Tracked {
    const v = typeof key === "string" ? this.views.get(key) : undefined;
    if (!v) throw new ControlError(`sessão não encontrada: ${String(key)} (veja \`amh app\`)`);
    return v;
  }

  list() {
    return [...this.views.entries()].map(([key, v]) => ({
      key, label: v.label, agentId: v.agentId, status: v.status, cwd: v.cwd, branch: v.branch ?? null,
      pending: v.permOptions.size,
    }));
  }

  show(key: unknown, n: unknown) {
    const v = this.view(key);
    const count = typeof n === "number" && Number.isInteger(n) && n > 0 ? Math.min(n, SHOW_MAX) : SHOW_DEFAULT;
    const entries = v.timeline.entries;
    return {
      key, label: v.label, status: v.status, total: entries.length,
      entries: entries.slice(-count).map(entryText),
    };
  }

  permissions() {
    const out = [];
    for (const [key, v] of this.views) {
      for (const [requestId, options] of v.permOptions) {
        const entry = v.timeline.entries.find((e) => e.id === v.permEntry.get(requestId));
        out.push({
          key, requestId, label: v.label, cwd: v.cwd,
          title: entry?.kind === "permission" ? entry.title : requestId,
          options: options.map((o) => ({ optionId: o.optionId, name: o.name, kind: o.kind })),
        });
      }
    }
    return out;
  }

  respond(key: unknown, requestId: unknown, optionId: unknown) {
    const v = this.view(key);
    if (typeof requestId !== "string" || !v.permOptions.has(requestId)) {
      throw new ControlError(`pedido ${String(requestId)} não está pendente em ${String(key)} (já respondido?)`);
    }
    if (optionId !== null && (typeof optionId !== "string" || !v.permOptions.get(requestId)!.some((o) => o.optionId === optionId))) {
      throw new ControlError(`opção ${String(optionId)} não foi oferecida nesse pedido`);
    }
    this.answer(key as string, requestId, optionId as string | null);
    return { ok: true };
  }

  /** Uma chamada da rota /control. Erro de uso vira ControlError (400 na ponte). */
  handle(req: Record<string, unknown>): unknown {
    switch (req.cmd) {
      case "list": return this.list();
      case "show": return this.show(req.key, req.n);
      case "permissions": return this.permissions();
      case "answer": return this.respond(req.key, req.requestId, req.optionId);
      default: throw new ControlError(`comando desconhecido: ${String(req.cmd)}`);
    }
  }
}
