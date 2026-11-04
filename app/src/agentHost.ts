/**
 * Uma sessão com um agente ACP. Abre o processo (ou conecta num agente em memória, nos
 * testes), inicializa, cria a sessão no cwd da worktree e repassa tudo como eventos.
 *
 * Permissões nunca são decididas aqui: cada session/request_permission vira um evento
 * "permission" e fica pendente até o usuário responder (answer) ou o turno ser cancelado.
 * O cliente não anuncia fs nem terminal: o agente usa as próprias ferramentas, dentro das
 * regras de permissão dele, e o app só observa e aprova.
 */
import { spawn, type ChildProcess } from "node:child_process";
import { Readable, Writable } from "node:stream";
import {
  client, methods, ndJsonStream, PROTOCOL_VERSION,
  type AgentApp, type ClientConnection, type PermissionOption, type SessionModeState, type SessionUpdate, type StopReason, type Stream,
} from "@agentclientprotocol/sdk";
import type { LaunchSpec } from "./agents";

export type HostEvent =
  | { type: "ready"; sessionId: string; agentName?: string; modes: SessionModeState | null }
  | { type: "mode"; modeId: string }
  | { type: "update"; update: SessionUpdate }
  | { type: "permission"; requestId: string; title: string; options: PermissionOption[] }
  | { type: "permission_closed"; requestId: string; outcome: string }
  | { type: "stop"; reason: StopReason }
  | { type: "error"; message: string }
  | { type: "exit"; code: number | null; signal: string | null; stderrTail: string };

const STDERR_TAIL = 4000;
/** Ids de modo que pedem aprovação, por ordem de preferência (Claude: default; Codex: read-only/auto…). */
export const ASKING_MODES = ["default", "ask", "read-only"];

interface Pending {
  resolve: (optionId: string | null) => void;
  options: PermissionOption[];
}

export class AgentHost {
  private conn?: ClientConnection;
  private sessionId?: string;
  private pending = new Map<string, Pending>();
  private seq = 0;
  private busy = false;
  child?: ChildProcess;

  constructor(private readonly emit: (e: HostEvent) => void) {}

  /** Abre o processo do agente e conecta. */
  async launch(spec: LaunchSpec, cwd: string): Promise<string> {
    const child = spawn(spec.command, spec.args, { cwd, env: spec.env, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    let stderr = "";
    child.stderr?.on("data", (b: Buffer) => { stderr = (stderr + b.toString()).slice(-STDERR_TAIL); });
    const spawned = new Promise<void>((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", (err) => reject(new Error(`não abriu ${spec.command}: ${err.message}`)));
    });
    child.once("exit", (code, signal) => {
      this.failPending("processo do agente encerrou");
      this.emit({ type: "exit", code, signal, stderrTail: stderr });
    });
    await spawned;
    const stream = ndJsonStream(
      Writable.toWeb(child.stdin!) as WritableStream<Uint8Array>,
      Readable.toWeb(child.stdout!) as ReadableStream<Uint8Array>,
    );
    return this.connect(stream, cwd);
  }

  /** Conecta num stream ou num agente em memória e cria a sessão. Devolve o sessionId. */
  async connect(target: Stream | AgentApp, cwd: string): Promise<string> {
    const app = client({ name: "agent-console" })
      .onNotification(methods.client.session.update, ({ params }) => {
        if (params.sessionId === this.sessionId || this.sessionId === undefined) {
          this.emit({ type: "update", update: params.update });
        }
      })
      .onRequest(methods.client.session.requestPermission, ({ params, signal }) =>
        this.askUser(params.toolCall.title ?? params.toolCall.toolCallId, params.options, signal));
    this.conn = app.connect(target as Stream);
    const init = await this.conn.agent.request(methods.agent.initialize, {
      protocolVersion: PROTOCOL_VERSION,
      clientCapabilities: {},
      clientInfo: { name: "agent-console", version: "0.1.0" },
    });
    const session = await this.conn.agent.request(methods.agent.session.new, { cwd, mcpServers: [] });
    this.sessionId = session.sessionId;
    this.modes = session.modes ?? null;
    this.emit({ type: "ready", sessionId: session.sessionId, agentName: init.agentInfo?.name, modes: this.modes });
    return session.sessionId;
  }

  /** Modos que o agente oferece (ex. Claude: default, acceptEdits, plan, auto). */
  modes: SessionModeState | null = null;

  /** Troca o modo da sessão. Só aceita um modo que o agente anunciou. */
  async setMode(modeId: string): Promise<void> {
    if (!this.conn || !this.sessionId) throw new Error("sessão não iniciada");
    if (!this.modes?.availableModes.some((m) => m.id === modeId)) {
      throw new Error(`o agente não oferece o modo ${modeId}`);
    }
    await this.conn.agent.request(methods.agent.session.setMode, { sessionId: this.sessionId, modeId });
    this.modes = { ...this.modes, currentModeId: modeId };
    this.emit({ type: "mode", modeId });
  }

  /**
   * Modo que pede permissão antes de agir, se o agente tiver um: o app abre toda sessão
   * nele, porque a configuração do próprio agente pode aprovar sozinha (ex. Claude em
   * defaultMode "auto"). Devolve o modo escolhido, ou null se o agente não tem modos.
   */
  async useAskingMode(): Promise<string | null> {
    const ids = this.modes?.availableModes.map((m) => m.id) ?? [];
    const pick = ASKING_MODES.find((m) => ids.includes(m));
    if (!pick) return null;
    if (this.modes?.currentModeId !== pick) await this.setMode(pick);
    return pick;
  }

  private askUser(title: string, options: PermissionOption[], signal: AbortSignal) {
    const requestId = `p${++this.seq}`;
    return new Promise<{ outcome: { outcome: "cancelled" } | { outcome: "selected"; optionId: string } }>((resolve) => {
      const finish = (optionId: string | null) => {
        if (!this.pending.delete(requestId)) return;
        this.emit({ type: "permission_closed", requestId, outcome: optionId ?? "cancelled" });
        resolve({ outcome: optionId ? { outcome: "selected", optionId } : { outcome: "cancelled" } });
      };
      this.pending.set(requestId, { resolve: finish, options });
      signal.addEventListener("abort", () => finish(null), { once: true });
      this.emit({ type: "permission", requestId, title, options });
    });
  }

  /** Resposta do usuário a um pedido de permissão. optionId null = cancelar. */
  answer(requestId: string, optionId: string | null): void {
    const p = this.pending.get(requestId);
    if (!p) throw new Error(`pedido de permissão ${requestId} não está pendente`);
    if (optionId !== null && !p.options.some((o) => o.optionId === optionId)) {
      throw new Error(`opção ${optionId} não foi oferecida pelo agente`);
    }
    p.resolve(optionId);
  }

  private failPending(_why: string) {
    for (const p of [...this.pending.values()]) p.resolve(null);
  }

  async prompt(text: string): Promise<StopReason> {
    if (!this.conn || !this.sessionId) throw new Error("sessão não iniciada");
    if (this.busy) throw new Error("o agente ainda está no turno anterior");
    if (!text.trim()) throw new Error("mensagem vazia");
    this.busy = true;
    try {
      const res = await this.conn.agent.request(methods.agent.session.prompt, {
        sessionId: this.sessionId,
        prompt: [{ type: "text", text }],
      });
      this.emit({ type: "stop", reason: res.stopReason });
      return res.stopReason;
    } catch (err) {
      this.emit({ type: "error", message: err instanceof Error ? err.message : String(err) });
      throw err;
    } finally {
      this.busy = false;
    }
  }

  /** Cancela o turno: o protocolo exige responder 'cancelled' aos pedidos de permissão abertos. */
  async cancel(): Promise<void> {
    if (!this.conn || !this.sessionId) return;
    this.failPending("turno cancelado");
    await this.conn.agent.notify(methods.agent.session.cancel, { sessionId: this.sessionId });
  }

  get isBusy(): boolean {
    return this.busy;
  }

  get pendingPermissions(): string[] {
    return [...this.pending.keys()];
  }

  close(): void {
    this.failPending("sessão fechada");
    this.conn?.close();
    if (this.child && this.child.exitCode === null) this.child.kill("SIGTERM");
  }
}
