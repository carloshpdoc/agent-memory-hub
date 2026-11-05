/**
 * Parceiro: uma sessão (coordenador) delega uma tarefa a outro agente (worker) pelas ferramentas
 * MCP do console. Regras tiradas do Orca:
 * - a tarefa vai autossuficiente (o que fazer, quando está pronta, o que pode editar);
 * - o coordenador encerra o turno e é acordado com o resultado, nunca fica verificando;
 * - o worker devolve com evidência (concluir) ou pergunta (perguntar); falar com ele é por mensagem;
 * - fim de turno sem concluir não prova nada: o worker é lembrado, e depois de MAX_NUDGES
 *   tentativas a delegação falha e o coordenador é avisado.
 *
 * Esta classe só decide; abrir sessão, mandar mensagem e avisar a tela vêm de fora (deps),
 * para os testes rodarem sem agente nem janela.
 */
export type Role = "coordenador" | "worker" | "terminal";

/** Chave das sessões de terminal (fora do app): o app não as acorda, a chamada espera. */
export const TERMINAL_PREFIX = "t-";
export const WAIT_MS = 20 * 60_000;

/** Nome do servidor nas sessões abertas pelo app. Diferente do registro do terminal: com o
 * mesmo nome, a config do próprio agente (ex. ~/.codex/config.toml) vence e o worker fica
 * sem concluir/perguntar (medido com o Codex). */
export const PARTNER_SERVER = "agent-console-app";
/** Nome do registro do usuário no Claude/Codex do terminal (scripts/install_mcp.py). */
export const TERMINAL_SERVER = "agent-console";
export const MAX_NUDGES = 3;
export const MAX_ACTIVE = 3;
const TEXT_MAX = 8000;

type Schema = { type: "object"; properties: Record<string, unknown>; required: string[]; additionalProperties: false };
export interface ToolDef { name: string; description: string; inputSchema: Schema }

const str = (description: string) => ({ type: "string", description });

const COORDINATOR_TOOLS: ToolDef[] = [
  {
    name: "delegar",
    description:
      "Abre outro agente na mesma pasta e entrega uma tarefa a ele. Depois de chamar, ENCERRE o seu turno: " +
      "o resultado chega como uma nova mensagem quando o outro agente concluir ou perguntar algo. Não fique verificando.",
    inputSchema: {
      type: "object",
      properties: {
        agente: { type: "string", enum: ["claude", "codex", "cursor"], description: "Quem faz a tarefa." },
        tarefa: str("A tarefa completa, autossuficiente: o outro agente não vê a sua conversa."),
        pronto_quando: str("Como saber que terminou (ex.: testes X passam, arquivo Y existe)."),
        pode_editar: { type: "array", items: { type: "string" }, description: "Arquivos ou pastas que pode alterar. Vazio = só leitura." },
      },
      required: ["agente", "tarefa", "pronto_quando"],
      additionalProperties: false,
    },
  },
  {
    name: "responder",
    description: "Responde a uma pergunta de um agente a quem você delegou. Depois, encerre o seu turno.",
    inputSchema: {
      type: "object",
      properties: { delegacao: str("O id da delegação (ex.: d1)."), resposta: str("A resposta.") },
      required: ["delegacao", "resposta"],
      additionalProperties: false,
    },
  },
];

const WORKER_TOOLS: ToolDef[] = [
  {
    name: "concluir",
    description: "Entrega o resultado da tarefa delegada a quem pediu. Chame uma vez, no fim, e encerre o turno.",
    inputSchema: {
      type: "object",
      properties: {
        status: { type: "string", enum: ["feito", "bloqueado"], description: "feito = critério de pronto atingido." },
        resumo: str("O que foi feito, ou por que não deu."),
        evidencias: { type: "array", items: { type: "string" }, description: "Comandos rodados com a saída, arquivos alterados." },
      },
      required: ["status", "resumo", "evidencias"],
      additionalProperties: false,
    },
  },
  {
    name: "perguntar",
    description: "Pergunta algo a quem delegou a tarefa. Depois, encerre o turno: a resposta chega como nova mensagem.",
    inputSchema: {
      type: "object",
      properties: { pergunta: str("A pergunta, com o contexto necessário para responder.") },
      required: ["pergunta"],
      additionalProperties: false,
    },
  },
];

const WAITS = `A chamada espera até ${WAIT_MS / 60_000} min pelo resultado (o outro agente roda no Agent Console, onde as permissões dele são aprovadas); se o tempo acabar, chame \`aguardar\`.`;

/** No terminal o app não acorda a sessão: delegar e responder esperam o retorno. */
const TERMINAL_TOOLS: ToolDef[] = [
  { ...COORDINATOR_TOOLS[0], description: `Abre outro agente no Agent Console, na mesma pasta, e entrega uma tarefa a ele. ${WAITS} O retorno é o resultado ou uma pergunta do outro agente.` },
  { ...COORDINATOR_TOOLS[1], description: `Responde a uma pergunta de um agente a quem você delegou. ${WAITS}` },
  {
    name: "aguardar",
    description: `Continua esperando o retorno de uma delegação em andamento. ${WAITS}`,
    inputSchema: { type: "object", properties: {}, required: [], additionalProperties: false },
  },
];

/** Ferramentas que uma sessão vê. O worker não delega (sem cadeia de delegações). */
export function toolsFor(role: Role): ToolDef[] {
  return role === "worker" ? WORKER_TOOLS : role === "terminal" ? TERMINAL_TOOLS : COORDINATOR_TOOLS;
}

export type DelegationState = "trabalhando" | "perguntou" | "concluida" | "bloqueada" | "falhou";

export interface Delegation {
  id: string;
  from: string;
  agent: string;
  to?: string;
  task: string;
  doneWhen: string;
  canEdit: string[];
  state: DelegationState;
  nudges: number;
  /** O worker chamou concluir/perguntar neste turno (fim de turno sem isso = lembrete). */
  reported: boolean;
}

export interface PartnerDeps {
  /** Abre o agente na pasta do coordenador como worker; devolve a chave da sessão. */
  open(agent: string, fromKey: string, delegationId: string): Promise<string>;
  label(key: string): string;
  cwd(key: string): string;
  busy(key: string): boolean;
  /** Manda uma mensagem à sessão como se fosse do usuário, marcada como vinda do parceiro. */
  prompt(key: string, text: string): void;
  notice(key: string, text: string): void;
}

export interface ToolReply { ok: boolean; text: string }

function clip(v: unknown, max = TEXT_MAX): string {
  const t = typeof v === "string" ? v.trim() : "";
  return t.length <= max ? t : `${t.slice(0, max)}… [+${t.length - max} caracteres]`;
}

export function taskSpec(d: Delegation, fromLabel: string, cwd: string): string {
  const edit = d.canEdit.length
    ? d.canEdit.map((p) => `- ${p}`).join("\n")
    : "Só leitura: não altere nenhum arquivo.";
  return [
    `# Tarefa delegada por ${fromLabel} (delegação ${d.id})`,
    `Pasta: \`${cwd}\`. Você não vê a conversa de quem pediu: tudo o que precisa está aqui.`,
    `Responder em texto não entrega nada: o resultado só chega a quem pediu pela ferramenta \`concluir\` (servidor ${PARTNER_SERVER}).`,
    "", "## Tarefa", d.task,
    "", "## Pronto quando", d.doneWhen,
    "", "## Pode editar", edit,
    "", "## Como devolver",
    `Ao terminar, chame a ferramenta \`concluir\` (servidor ${PARTNER_SERVER}) com status, resumo e evidências: comandos rodados com a saída e arquivos alterados. Depois encerre o turno.`,
    `Se faltar informação para continuar, chame \`perguntar\` e encerre o turno; a resposta chega como nova mensagem.`,
  ].join("\n");
}

export class Partner {
  private delegations = new Map<string, Delegation>();
  private byWorker = new Map<string, string>();
  /** Mensagens para coordenadores ocupados: entregues juntas no próximo fim de turno. */
  private queued = new Map<string, string[]>();
  private seq = 0;

  constructor(private readonly deps: PartnerDeps) {}

  roleOf(key: string): Role {
    return this.byWorker.has(key) ? "worker" : key.startsWith(TERMINAL_PREFIX) ? "terminal" : "coordenador";
  }

  /** Delegações de uma sessão ainda em andamento (aguardar sem nenhuma não espera à toa). */
  activeFrom(key: string): number {
    return [...this.delegations.values()].filter((d) => d.from === key && (d.state === "trabalhando" || d.state === "perguntou")).length;
  }

  list(): Delegation[] {
    return [...this.delegations.values()].map((d) => ({ ...d, canEdit: [...d.canEdit] }));
  }

  /** Uma chamada de ferramenta vinda do servidor MCP de uma sessão. Nunca lança: erro vira resposta. */
  async call(key: string, tool: string, args: Record<string, unknown>): Promise<ToolReply> {
    try {
      const allowed = toolsFor(this.roleOf(key)).map((t) => t.name);
      if (!allowed.includes(tool)) return { ok: false, text: `ferramenta ${tool} não está disponível para esta sessão` };
      switch (tool) {
        case "delegar": return await this.delegate(key, args);
        case "responder": return this.answer(key, args);
        case "concluir": return this.finish(key, args);
        case "perguntar": return this.ask(key, args);
        case "aguardar": return { ok: true, text: "" };    // a espera é de quem chamou (waitFor)
      }
      return { ok: false, text: `ferramenta desconhecida: ${tool}` };
    } catch (err) {
      return { ok: false, text: err instanceof Error ? err.message : String(err) };
    }
  }

  private async delegate(from: string, args: Record<string, unknown>): Promise<ToolReply> {
    const agent = clip(args.agente, 20);
    const task = clip(args.tarefa);
    const doneWhen = clip(args.pronto_quando, 2000);
    const canEdit = Array.isArray(args.pode_editar) ? args.pode_editar.filter((p): p is string => typeof p === "string" && !!p.trim()) : [];
    if (!task || !doneWhen) return { ok: false, text: "tarefa e pronto_quando são obrigatórios" };
    const active = this.activeFrom(from);
    if (active >= MAX_ACTIVE) return { ok: false, text: `já há ${active} delegações em andamento; espere uma terminar` };

    const d: Delegation = { id: `d${++this.seq}`, from, agent, task, doneWhen, canEdit, state: "trabalhando", nudges: 0, reported: false };
    const to = await this.deps.open(agent, from, d.id);       // lança se o agente não abre: vira resposta de erro
    d.to = to;
    this.delegations.set(d.id, d);
    this.byWorker.set(to, d.id);
    this.deps.notice(from, `delegação ${d.id} aberta em ${this.deps.label(to)}`);
    this.deps.prompt(to, taskSpec(d, this.deps.label(from), this.deps.cwd(from)));
    const next = this.roleOf(from) === "terminal"
      ? "Esperando o resultado."
      : "Encerre o seu turno agora: o resultado chega como nova mensagem. Não fique verificando.";
    return { ok: true, text: `Delegação ${d.id} aberta em ${this.deps.label(to)}. ${next}` };
  }

  private answer(from: string, args: Record<string, unknown>): ToolReply {
    const d = this.delegations.get(clip(args.delegacao, 20));
    const text = clip(args.resposta);
    if (!d || d.from !== from) return { ok: false, text: "delegação não encontrada nesta sessão" };
    if (d.state !== "perguntou") return { ok: false, text: `a delegação ${d.id} não está esperando resposta (estado: ${d.state})` };
    if (!text) return { ok: false, text: "resposta vazia" };
    d.state = "trabalhando";
    d.nudges = 0;
    this.send(d.to!, `Resposta de ${this.deps.label(from)} (delegação ${d.id}):\n\n${text}`);
    return { ok: true, text: "Resposta entregue. Encerre o seu turno." };
  }

  private finish(worker: string, args: Record<string, unknown>): ToolReply {
    const d = this.delegations.get(this.byWorker.get(worker)!)!;
    if (d.state !== "trabalhando") return { ok: false, text: `a delegação ${d.id} já está ${d.state}` };
    const status = args.status === "bloqueado" ? "bloqueado" : args.status === "feito" ? "feito" : null;
    const summary = clip(args.resumo);
    const evidence = Array.isArray(args.evidencias) ? args.evidencias.map((e) => clip(e, 2000)).filter(Boolean) : [];
    if (!status || !summary) return { ok: false, text: "status (feito|bloqueado) e resumo são obrigatórios" };
    if (status === "feito" && evidence.length === 0) {
      return { ok: false, text: "status feito exige evidências: comandos rodados com a saída, arquivos alterados" };
    }
    d.state = status === "feito" ? "concluida" : "bloqueada";
    d.reported = true;
    this.send(d.from, [
      `Resultado da delegação ${d.id} (${this.deps.label(worker)}): ${status}`,
      "", summary,
      ...(evidence.length ? ["", "Evidências:", ...evidence.map((e) => `- ${e}`)] : []),
      "", "Confira o que precisar antes de seguir: isto é o relato do outro agente.",
    ].join("\n"));
    return { ok: true, text: "Resultado entregue. Encerre o seu turno." };
  }

  private ask(worker: string, args: Record<string, unknown>): ToolReply {
    const d = this.delegations.get(this.byWorker.get(worker)!)!;
    const question = clip(args.pergunta);
    if (d.state !== "trabalhando") return { ok: false, text: `a delegação ${d.id} está ${d.state}` };
    if (!question) return { ok: false, text: "pergunta vazia" };
    d.state = "perguntou";
    d.reported = true;
    this.send(d.from, `Pergunta da delegação ${d.id} (${this.deps.label(worker)}):\n\n${question}\n\nResponda com a ferramenta \`responder\` (delegacao: "${d.id}").`);
    return { ok: true, text: "Pergunta enviada. Encerre o seu turno: a resposta chega como nova mensagem." };
  }

  /** Fim de turno de qualquer sessão: entrega o que esperava e cobra o worker que não reportou. */
  onStop(key: string, reason: string): void {
    this.flush(key);
    const id = this.byWorker.get(key);
    if (!id) return;
    const d = this.delegations.get(id)!;
    if (d.state !== "trabalhando") return;
    if (d.reported) { d.reported = false; return; }
    if (reason === "cancelled") return this.fail(d, "o turno do worker foi cancelado");
    d.nudges++;
    if (d.nudges >= MAX_NUDGES) return this.fail(d, `encerrou ${d.nudges} turnos sem chamar concluir`);
    this.send(key, `Você encerrou o turno sem chamar \`concluir\` nem \`perguntar\` (delegação ${d.id}). Termine a tarefa e chame \`concluir\` com as evidências, ou \`perguntar\` se faltar algo. Tentativa ${d.nudges + 1} de ${MAX_NUDGES}.`);
  }

  /** O processo de uma sessão encerrou ou a sessão foi fechada. */
  onGone(key: string, why: string): void {
    const id = this.byWorker.get(key);
    if (id) {
      const d = this.delegations.get(id)!;
      if (d.state === "trabalhando" || d.state === "perguntou") this.fail(d, why);
    }
    this.queued.delete(key);
  }

  private fail(d: Delegation, why: string) {
    d.state = "falhou";
    this.send(d.from, `A delegação ${d.id} (${d.to ? this.deps.label(d.to) : d.agent}) falhou: ${why}. Nada foi entregue; confira o estado real antes de seguir.`);
  }

  /** Acorda a sessão agora, ou na próxima vez que ela terminar o turno. */
  private send(key: string, text: string) {
    if (!this.deps.busy(key)) { this.deps.prompt(key, text); return; }
    this.queued.set(key, [...(this.queued.get(key) ?? []), text]);
  }

  private flush(key: string) {
    const msgs = this.queued.get(key);
    if (!msgs?.length || this.deps.busy(key)) return;
    this.queued.delete(key);
    this.deps.prompt(key, msgs.join("\n\n---\n\n"));
  }
}

const ANCESTOR_DEPTH = 32;

/**
 * Este processo roda dentro de uma sessão aberta pelo app? Pelo ambiente não basta: o Codex
 * não repassa variáveis ao servidor MCP (medido). Então: o app é um processo-ancestral.
 */
export function underApp(appPid: unknown, start: number, parentOf: (pid: number) => number | null): boolean {
  if (typeof appPid !== "number") return false;
  let pid = start;
  for (let i = 0; i < ANCESTOR_DEPTH && pid > 1; i++) {
    if (pid === appPid) return true;
    const next = parentOf(pid);
    if (next === null) return false;
    pid = next;
  }
  return false;
}

/** Entrada mcpServers do session/new: o agente roda o partnerMcp.js com o binário do próprio app. */
export function partnerServerEntry(
  script: string, conn: { port: number; token: string }, key: string, role: Role,
): { name: string; command: string; args: string[]; env: { name: string; value: string }[] } {
  return {
    name: PARTNER_SERVER,
    command: process.execPath,
    args: [script],
    env: [
      { name: "ELECTRON_RUN_AS_NODE", value: "1" },
      { name: "AGENT_CONSOLE_PARTNER_URL", value: `http://127.0.0.1:${conn.port}/partner` },
      { name: "AGENT_CONSOLE_PARTNER_TOKEN", value: conn.token },
      { name: "AGENT_CONSOLE_PARTNER_KEY", value: key },
      { name: "AGENT_CONSOLE_PARTNER_ROLE", value: role },
    ],
  };
}

/**
 * Caixa de entrada das sessões de terminal: o Partner "manda mensagem" para elas e quem está
 * esperando numa chamada recebe. Mensagem que chega sem ninguém esperando fica guardada.
 */
export class Mailbox {
  private inbox = new Map<string, string[]>();
  private waiters = new Map<string, (text: string | null) => void>();

  deliver(key: string, text: string): void {
    const w = this.waiters.get(key);
    if (w) { this.waiters.delete(key); w(text); return; }
    this.inbox.set(key, [...(this.inbox.get(key) ?? []), text]);
  }

  /** O que já chegou, ou a próxima mensagem; null se o prazo acabar. Uma espera por chave. */
  wait(key: string, ms: number): Promise<string | null> {
    const ready = this.inbox.get(key);
    if (ready?.length) { this.inbox.delete(key); return Promise.resolve(ready.join("\n\n---\n\n")); }
    this.waiters.get(key)?.(null);                       // espera antiga (chamada abandonada) cede
    return new Promise((resolve) => {
      const timer = setTimeout(() => { if (this.waiters.get(key) === done) this.waiters.delete(key); resolve(null); }, ms);
      const done = (text: string | null) => { clearTimeout(timer); resolve(text); };
      this.waiters.set(key, done);
    });
  }
}

/** Uma chamada vinda do terminal: faz a ação e espera o retorno da delegação na mesma chamada. */
export async function terminalCall(
  partner: Partner, mail: Mailbox, key: string, tool: string, args: Record<string, unknown>, waitMs = WAIT_MS,
): Promise<ToolReply> {
  const r = await partner.call(key, tool, args);
  if (!r.ok) return r;
  if (tool === "aguardar" && partner.activeFrom(key) === 0) {
    const left = await mail.wait(key, 0);                // nada em andamento: só o que sobrou na caixa
    return { ok: true, text: left ?? "Nenhuma delegação em andamento." };
  }
  const got = await mail.wait(key, waitMs);
  const head = r.text ? `${r.text}\n\n` : "";
  return got === null
    ? { ok: true, text: `${head}Ainda em andamento depois de ${Math.round(waitMs / 60_000)} min. Chame \`aguardar\` para continuar esperando.` }
    : { ok: true, text: `${head}${got}` };
}

/** Sessões de terminal que delegaram: o app só sabe a pasta e o nome delas. */
export type TerminalSessions = Map<string, { cwd: string; label: string }>;

/**
 * Uma chamada do servidor MCP: de sessão do app vai direto ao Partner; do terminal registra a
 * sessão (pasta, nome) e espera o retorno na mesma chamada.
 */
export async function routePartnerCall(
  partner: Partner, mail: Mailbox, terminals: TerminalSessions,
  c: { key: string; tool: string; args: Record<string, unknown>; cwd?: string; from?: string },
  agentLabel: (id: string) => string, waitMs = WAIT_MS,
): Promise<ToolReply> {
  if (!c.key.startsWith(TERMINAL_PREFIX)) return partner.call(c.key, c.tool, c.args);
  if (!c.cwd) return { ok: false, text: "chamada do terminal sem a pasta do agente" };
  let name = "agente";
  try { if (c.from) name = agentLabel(c.from); } catch { /* origem desconhecida: nome genérico */ }
  terminals.set(c.key, { cwd: c.cwd, label: `${name} (terminal)` });
  return terminalCall(partner, mail, c.key, c.tool, c.args, waitMs);
}
