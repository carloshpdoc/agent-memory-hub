/**
 * Teste de fumaça contra um agente real, sem janela:
 *   npm run smoke -- <claude|codex|cursor> <pasta> "<mensagem>" [--allow]
 * Imprime cada evento numa linha. Permissões são RECUSADAS por padrão; --allow escolhe a
 * primeira opção allow_once. Sai com 1 se o turno não terminar em end_turn.
 */
import { agentSpec } from "./agents";
import { AgentHost, type HostEvent } from "./agentHost";

async function main() {
  const [id, cwd, text, flag] = process.argv.slice(2);
  if (!id || !cwd || !text) {
    console.error('uso: smoke <claude|codex|cursor> <pasta> "<mensagem>" [--allow]');
    process.exit(2);
  }
  const counts: Record<string, number> = {};
  let host: AgentHost;
  const onEvent = (e: HostEvent) => {
    const key = e.type === "update" ? `update:${e.update.sessionUpdate}` : e.type;
    counts[key] = (counts[key] ?? 0) + 1;
    if (e.type === "update") {
      const u = e.update as { sessionUpdate: string; content?: { text?: string }; title?: string; status?: string };
      const detail = u.content?.text ?? [u.title, u.status].filter(Boolean).join(" · ");
      if (u.sessionUpdate !== "agent_message_chunk" && u.sessionUpdate !== "agent_thought_chunk") {
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
  process.exit(reason === "end_turn" ? 0 : 1);
}

main();
