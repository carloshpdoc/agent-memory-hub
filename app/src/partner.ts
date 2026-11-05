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
export type Role = "coordenador" | "worker";

export const PARTNER_SERVER = "agent-console";
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

/** Ferramentas que uma sessão vê. O worker não delega (sem cadeia de delegações). */
export function toolsFor(role: Role): ToolDef[] {
  return role === "worker" ? WORKER_TOOLS : COORDINATOR_TOOLS;
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
    return this.byWorker.has(key) ? "worker" : "coordenador";
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
    const active = [...this.delegations.values()].filter((d) => d.from === from && (d.state === "trabalhando" || d.state === "perguntou"));
    if (active.length >= MAX_ACTIVE) return { ok: false, text: `já há ${active.length} delegações em andamento; espere uma terminar` };

    const d: Delegation = { id: `d${++this.seq}`, from, agent, task, doneWhen, canEdit, state: "trabalhando", nudges: 0, reported: false };
    const to = await this.deps.open(agent, from, d.id);       // lança se o agente não abre: vira resposta de erro
    d.to = to;
    this.delegations.set(d.id, d);
    this.byWorker.set(to, d.id);
    this.deps.notice(from, `delegação ${d.id} aberta em ${this.deps.label(to)}`);
    this.deps.prompt(to, taskSpec(d, this.deps.label(from), this.deps.cwd(from)));
    return {
      ok: true,
      text: `Delegação ${d.id} aberta em ${this.deps.label(to)}. Encerre o seu turno agora: o resultado chega como nova mensagem. Não fique verificando.`,
    };
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
