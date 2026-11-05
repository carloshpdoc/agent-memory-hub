/**
 * Pacote de continuação: o que um agente precisa para assumir uma sessão de outro, montado
 * da linha do tempo, sem chamar modelo (funciona com a ferramenta de origem sem tokens).
 *
 * Formato híbrido: resumo determinístico no topo (pedido original, último pedido, plano,
 * arquivos alterados, comandos, falhas), os últimos turnos na íntegra no fim, até caber no
 * orçamento, e um ponteiro para o histórico completo no hub.
 */
import type { Entry, Timeline } from "./timeline";

export interface HandoffSource {
  timeline: Timeline;
  fromLabel: string;
  toLabel: string;
  cwd: string;
  branch?: string;
  sessionId?: string;
  /** O turno de origem ainda estava em andamento quando a troca foi pedida. */
  interrupted?: boolean;
}

export const HANDOFF_BUDGET = 16000;      // caracteres do pacote inteiro
const REQUEST_MAX = 1500;
const ENTRY_MAX = 3000;
const LIST_MAX = 30;
const COMMANDS_MAX = 15;

type Tool = Extract<Entry, { kind: "tool" }>;

function clip(text: string, max: number): string {
  const t = text.trim();
  return t.length <= max ? t : `${t.slice(0, max)}… [+${t.length - max} caracteres]`;
}

/** Linhas como o editor conta: a quebra no fim do arquivo não abre uma linha a mais. */
const countLines = (text: string) => (text ? text.replace(/\n$/, "").split("\n").length : 0);

function lineDelta(oldText: string | null | undefined, newText: string): string {
  const before = countLines(oldText ?? "");
  const after = countLines(newText);
  return oldText ? `${before}→${after} linhas` : `novo, ${after} linhas`;
}

function capList(items: string[], max: number): string[] {
  return items.length <= max ? items : [...items.slice(0, max), `… e mais ${items.length - max}`];
}

/** Uma entrada como aparece em "últimos turnos"; null = não entra (pensamento, aviso vazio). */
function turnLine(e: Entry): string | null {
  switch (e.kind) {
    case "user": return `### Usuário\n${clip(e.text, ENTRY_MAX)}`;
    case "agent": return e.text.trim() ? `### Agente\n${clip(e.text, ENTRY_MAX)}` : null;
    case "tool": return `- [ferramenta] ${e.title} (${e.status})${e.locations.length ? `: ${e.locations.slice(0, 3).join(", ")}` : ""}`;
    case "permission": return `- [permissão] ${e.title}: ${e.answer ?? "sem resposta"}`;
    case "stop": return `- [fim do turno: ${e.reason}]`;
    case "notice": return e.text.trim() ? `- [aviso] ${clip(e.text, 300)}` : null;
    case "thought": return null;
  }
}

export function buildHandoff(src: HandoffSource, budget = HANDOFF_BUDGET): string {
  const { entries, plan } = src.timeline;
  const users = entries.filter((e): e is Extract<Entry, { kind: "user" }> => e.kind === "user");
  const tools = entries.filter((e): e is Tool => e.kind === "tool");

  const changed = new Map<string, string>();
  for (const t of tools) for (const d of t.diffs) changed.set(d.path, lineDelta(d.oldText, d.newText));
  const seen = [...new Set(tools.flatMap((t) => t.locations))].filter((p) => !changed.has(p));
  const commands = tools.filter((t) => t.toolKind === "execute").map((t) => `${t.title} (${t.status})`);
  const failed = tools.filter((t) => t.status === "failed").map((t) => t.title);

  const head: string[] = [
    `# Continuação de trabalho: ${src.fromLabel} → ${src.toLabel}`,
    `Você está assumindo uma sessão que começou no ${src.fromLabel}. Pasta: \`${src.cwd}\`${src.branch ? `, branch \`${src.branch}\`` : ""}.`,
    "Confira o estado real (git status, os arquivos) antes de agir: este resumo foi montado da conversa e pode estar desatualizado. Não refaça o que já está feito.",
  ];
  if (src.interrupted) head.push("O último turno do agente anterior foi interrompido no meio: verifique se a última ação terminou.");
  if (users.length) head.push("", "## Pedido original", clip(users[0].text, REQUEST_MAX));
  if (users.length > 1) head.push("", "## Último pedido", clip(users[users.length - 1].text, REQUEST_MAX));
  if (plan.length) head.push("", "## Plano", ...plan.map((p) => `- [${p.status}] ${p.content}`));
  if (changed.size) head.push("", "## Arquivos alterados", ...capList([...changed].map(([p, d]) => `- ${p} (${d})`), LIST_MAX));
  if (seen.length) head.push("", "## Arquivos lidos ou citados", ...capList(seen.map((p) => `- ${p}`), LIST_MAX));
  if (commands.length) head.push("", "## Comandos rodados (os mais recentes)", ...commands.slice(-COMMANDS_MAX).map((c) => `- ${c}`));
  if (failed.length) head.push("", "## Falhas", ...capList(failed.map((f) => `- ${f}`), LIST_MAX));

  const tail: string[] = [];
  if (src.sessionId) {
    tail.push("", "## Histórico completo",
      `Sessão de origem: ${src.fromLabel}, id \`${src.sessionId}\`. Se precisar de algo que não está aqui, busque no agent-memory-hub (get_session com esse id).`);
  }

  const fixed = [...head, "", "## Últimos turnos (na íntegra)", ...tail].join("\n").length;
  const turns = entries.map(turnLine).filter((l): l is string => l !== null);
  const kept: string[] = [];
  let used = fixed;
  for (let i = turns.length - 1; i >= 0; i--) {
    const cost = turns[i].length + 1;
    if (used + cost > budget && kept.length > 0) break;          // o turno mais recente sempre entra
    kept.unshift(turns[i]);
    used += cost;
  }
  const omitted = turns.length - kept.length;
  const body = omitted ? [`(${omitted} item(ns) anteriores omitidos; estão no histórico completo)`, ...kept] : kept;
  return [...head, "", "## Últimos turnos (na íntegra)", ...body, ...tail].join("\n");
}
