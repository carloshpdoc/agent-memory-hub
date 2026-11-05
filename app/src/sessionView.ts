/**
 * Estado de uma sessão como o usuário a vê (status, linha do tempo, permissões pendentes),
 * montado dos eventos do AgentHost. Função pura e sem DOM: a tela e o controle pelo
 * terminal (control.ts) aplicam os mesmos eventos e por isso mostram o mesmo estado.
 */
import type { PermissionOption, SessionModeState } from "@agentclientprotocol/sdk";
import type { HostEvent } from "./agentHost";
import { addEntry, answerPermission, applyUpdate, emptyTimeline, type Timeline } from "./timeline";

export type Status = "iniciando" | "pronto" | "trabalhando" | "aguardando você" | "encerrado";

export interface SessionView {
  sessionId?: string;     // id da sessão no agente (aponta para o histórico no hub)
  modes: SessionModeState | null;
  timeline: Timeline;
  status: Status;
  permEntry: Map<string, string>;          // requestId -> id da entrada na timeline
  permOptions: Map<string, PermissionOption[]>;
}

export const newView = (modes: SessionModeState | null): SessionView => ({
  modes, timeline: emptyTimeline(), status: "iniciando", permEntry: new Map(), permOptions: new Map(),
});

export function modeName(s: SessionView, id: string): string {
  return s.modes?.availableModes.find((m) => m.id === id)?.name ?? id;
}

/** Mensagem do usuário enviada à sessão (pela tela ou pelo terminal). */
export function userSent(s: SessionView, text: string) {
  s.timeline = addEntry(s.timeline, { kind: "user", text });
  s.status = "trabalhando";
}

export function applyEvent(s: SessionView, ev: HostEvent) {
  switch (ev.type) {
    case "ready":
      s.status = "pronto";
      s.sessionId = ev.sessionId;
      s.modes = ev.modes ?? s.modes;
      break;
    case "mode":
      if (s.modes) s.modes = { ...s.modes, currentModeId: ev.modeId };
      s.timeline = addEntry(s.timeline, { kind: "notice", text: `modo: ${modeName(s, ev.modeId)}` });
      break;
    case "update":
      s.timeline = applyUpdate(s.timeline, ev.update);   // status muda só com o turno (envio/stop)
      break;
    case "permission": {
      s.timeline = addEntry(s.timeline, {
        kind: "permission", title: ev.title, options: ev.options.map((o) => `${o.name} (${o.kind})`),
      });
      s.permEntry.set(ev.requestId, s.timeline.entries[s.timeline.entries.length - 1].id);
      s.permOptions.set(ev.requestId, ev.options);
      s.status = "aguardando você";
      break;
    }
    case "permission_closed": {
      const id = s.permEntry.get(ev.requestId);
      const opt = s.permOptions.get(ev.requestId)?.find((o) => o.optionId === ev.outcome);
      if (id) s.timeline = answerPermission(s.timeline, id, opt ? opt.name : "cancelado");
      s.permOptions.delete(ev.requestId);
      if (s.status === "aguardando você" && s.permOptions.size === 0) s.status = "trabalhando";
      break;
    }
    case "injected":                                       // mensagem do parceiro, não digitada por você
      s.timeline = addEntry(s.timeline, { kind: "notice", text: "mensagem automática do parceiro:" });
      userSent(s, ev.text);
      break;
    case "notice":
      s.timeline = addEntry(s.timeline, { kind: "notice", text: ev.text });
      break;
    case "stop":
      s.timeline = addEntry(s.timeline, { kind: "stop", reason: ev.reason });
      s.status = "pronto";
      break;
    case "error":
      s.timeline = addEntry(s.timeline, { kind: "notice", text: `erro: ${ev.message}` });
      if (s.status !== "encerrado") s.status = "pronto";
      break;
    case "exit":
      s.timeline = addEntry(s.timeline, {
        kind: "notice",
        text: `processo do agente encerrou (código ${ev.code ?? "-"}${ev.signal ? `, sinal ${ev.signal}` : ""})` +
          (ev.stderrTail.trim() ? `\n${ev.stderrTail.trim().slice(-1500)}` : ""),
      });
      s.status = "encerrado";
      break;
  }
}
