/**
 * Teste de fumaça contra um agente real, sem janela:
 *   npm run smoke -- <claude|codex|cursor> <pasta> "<mensagem>" [--allow] [--continuar-em <agente> "<próximo passo>"]
 * Imprime cada evento numa linha. Permissões são RECUSADAS por padrão; --allow escolhe a
 * primeira opção allow_once. Sai com 1 se o turno não terminar em end_turn.
 * Com --continuar-em, monta o pacote de continuação da primeira sessão e abre o segundo
 * agente com ele + o próximo passo (o mesmo caminho do botão "Continuar" da janela).
 */
import { agentSpec } from "./agents";
import { AgentHost, type HostEvent } from "./agentHost";
import { buildHandoff } from "./handoff";
import { addEntry, applyUpdate, emptyTimeline, type Timeline } from "./timeline";

async function main() {
  const argv = process.argv.slice(2);
  const ci = argv.indexOf("--continuar-em");
  const next = ci >= 0 ? { agent: argv[ci + 1], text: argv[ci + 2] } : null;
  if (ci >= 0) argv.splice(ci, 3);
  if (next && (!next.agent || !next.text)) {
    console.error('uso: --continuar-em <agente> "<próximo passo>"');
    process.exit(2);
  }
  const [id, cwd, text, flag] = argv;
  const first = await runOne(id, cwd, text, flag);
  if (!next || first.reason !== "end_turn") process.exit(first.reason === "end_turn" ? 0 : 1);
  const pack = buildHandoff({
    timeline: first.timeline, fromLabel: agentSpec(id).label, toLabel: agentSpec(next.agent).label,
    cwd, sessionId: first.sessionId,
  });
  console.log(`\n===== pacote (${pack.length} caracteres) =====\n${pack}\n===== fim do pacote =====\n`);
  const second = await runOne(next.agent, cwd, `${pack}\n\n## Próximo passo\n${next.text}`, flag);
  process.exit(second.reason === "end_turn" ? 0 : 1);
}

async function runOne(id: string, cwd: string, text: string, flag: string | undefined) {
  if (!id || !cwd || !text) {
    console.error('uso: smoke <claude|codex|cursor> <pasta> "<mensagem>" [--allow]');
    process.exit(2);
  }
  const counts: Record<string, number> = {};
  let host: AgentHost;
  let timeline: Timeline = addEntry(emptyTimeline(), { kind: "user", text });
  let sessionId: string | undefined;
  const onEvent = (e: HostEvent) => {
    if (e.type === "update") timeline = applyUpdate(timeline, e.update);
    if (e.type === "ready") sessionId = e.sessionId;
    if (e.type === "stop") timeline = addEntry(timeline, { kind: "stop", reason: e.reason });
    const key = e.type === "update" ? `update:${e.update.sessionUpdate}` : e.type;
    counts[key] = (counts[key] ?? 0) + 1;
    if (e.type === "update") {
      const u = e.update as { sessionUpdate: string; content?: { text?: string }; title?: string; status?: string };
      const detail = u.content?.text ?? [u.title, u.status].filter(Boolean).join(" · ");
      if (process.env.SMOKE_RAW && u.sessionUpdate.startsWith("tool_call")) {
        console.log(`[${u.sessionUpdate} raw] ${JSON.stringify(u)}`);
      } else if (u.sessionUpdate !== "agent_message_chunk" && u.sessionUpdate !== "agent_thought_chunk") {
        console.log(`[${u.sessionUpdate}] ${String(detail).slice(0, 160)}`);
      } else {
        process.stdout.write(u.content?.text ?? "");
      }
    } else if (e.type === "permission") {
      const pick = flag === "--allow" ? e.options.find((o) => o.kind === "allow_once") : e.options.find((o) => o.kind.startsWith("reject"));
      console.log(`\n[permission] ${e.title} -> ${pick ? pick.name : "cancelar"}`);
      host.answer(e.requestId, pick ? pick.optionId : null);
    } else {
      console.log(`\n[${e.type}] ${JSON.stringify(e).slice(0, 300)}`);
    }
  };
  host = new AgentHost(onEvent);
  const t0 = Date.now();
  let reason = "error";
  try {
    await host.launch(agentSpec(id).launch(), cwd);
    const modes = host.modes;
    console.log(`[modos] atual=${modes?.currentModeId ?? "-"} disponíveis=${(modes?.availableModes ?? []).map((m) => m.id).join(",") || "nenhum"}`);
    const picked = await host.useAskingMode();
    console.log(`[modo usado] ${picked ?? "o do agente (sem modo que pede permissão)"}`);
    reason = await host.prompt(text);
  } catch (err) {
    console.error(`\nFALHOU: ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    host.close();
  }
  console.log(`\n--- ${id}: ${reason} em ${((Date.now() - t0) / 1000).toFixed(1)}s; eventos: ${JSON.stringify(counts)}`);
  return { reason, timeline, sessionId };
}

main();
