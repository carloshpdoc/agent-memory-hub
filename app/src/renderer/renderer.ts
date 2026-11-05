/**
 * Tela do console. Todo texto vindo do agente é dado não confiável: entra no DOM só por
 * textContent, nunca como HTML.
 */
import type { PermissionOption, SessionModeState } from "@agentclientprotocol/sdk";
import type { HostEvent } from "../agentHost";
import type { ConsoleApi } from "../preload";
import { foldExternal, type ExternalEvent, type ExternalSession } from "../external";
import { addEntry, answerPermission, applyUpdate, emptyTimeline, type Entry, type Timeline } from "../timeline";

declare global {
  interface Window { consoleApi: ConsoleApi }
}
const api = window.consoleApi;

type Status = "iniciando" | "pronto" | "trabalhando" | "aguardando você" | "encerrado";
interface Sess {
  key: string;
  label: string;
  cwd: string;
  branch?: string;
  modes: SessionModeState | null;
  timeline: Timeline;
  status: Status;
  permEntry: Map<string, string>;          // requestId -> id da entrada na timeline
  permOptions: Map<string, PermissionOption[]>;
  rendered: Map<string, { entry: Entry; el: HTMLElement }>;
}

const sessions = new Map<string, Sess>();
let active: string | null = null;
let repoDir: string | null = null;

const $ = <T extends HTMLElement = HTMLElement>(sel: string) => document.querySelector(sel) as T;
function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---- formulário de nova sessão -------------------------------------------------------
async function init() {
  const agents = (await api.listAgents()) as { id: string; label: string }[];
  for (const a of agents) $("#agent").append(new Option(a.label, a.id));
  $("#pick-repo").addEventListener("click", async () => {
    const dir = (await api.pickRepo()) as string | null;
    if (dir) { repoDir = dir; $("#repo-path").textContent = dir; }
  });
  $("#new-session").addEventListener("submit", onStart);
  $("#prompt-form").addEventListener("submit", (e) => { e.preventDefault(); sendPrompt(); });
  $("#prompt").addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Enter" && (e as KeyboardEvent).metaKey) { e.preventDefault(); sendPrompt(); }
  });
  $("#cancel").addEventListener("click", () => active && api.cancel(active));
  $("#close").addEventListener("click", closeActive);
  $("#mode").addEventListener("change", onModeChange);
  api.onEvent((msg) => onEvent(msg as { key: string; event: HostEvent }));
  api.onExternal((e) => { externalSessions = foldExternal(externalSessions, e as ExternalEvent); renderExternal(); });
  const snap = (await api.externalSnapshot()) as { events: ExternalEvent[]; dropped: number; live: boolean };
  for (const e of snap.events) externalSessions = foldExternal(externalSessions, e);
  externalNote = [snap.live ? "" : "ponte fora do ar", snap.dropped ? `${snap.dropped} linha(s) do spool ilegíveis` : ""]
    .filter(Boolean).join(" · ");
  renderExternal();
}

// ---- sessões fora do app (hooks/console_bridge.py) ----------------------------------
const EXTERNAL_SHOWN = 30;
let externalSessions = new Map<string, ExternalSession>();
let externalNote = "";

function ago(ts: number): string {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}min`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function renderExternal() {
  $("#external-note").textContent = externalNote;
  const list = [...externalSessions.values()].sort((a, b) => b.lastTs - a.lastTs).slice(0, EXTERNAL_SHOWN);
  const ul = $("#external");
  ul.replaceChildren(...list.map((s) => {
    const li = el("li", s.ended ? "ended" : "");
    li.title = `${s.sessionId}\n${s.cwd}`;
    const where = s.cwd.split("/").filter(Boolean).pop() ?? s.cwd;
    li.append(
      el("strong", "", `${s.tool === "claude" ? "Claude" : "Codex"} · ${where}`), el("br"),
      el("span", "mono muted", `${s.lastEvent}${s.lastDetail ? `: ${s.lastDetail}` : ""}`), el("br"),
      el("span", "muted", `${s.ended ? "encerrada" : "ativa"} · há ${ago(s.lastTs)} · ${s.events} evento(s)`),
    );
    return li;
  }));
  if (list.length === 0) ul.append(el("li", "muted", "nenhuma"));
}

function showFormError(text: string | null) {
  const p = $("#form-error");
  p.hidden = !text;
  p.textContent = text ?? "";
}

/** "Corrige o Login!" -> "corrige-o-login": o nome vira branch e pasta da worktree. */
function slug(text: string): string {
  return text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-").replace(/^[-._]+|-+$/g, "").slice(0, 48);
}

async function onStart(e: Event) {
  e.preventDefault();
  showFormError(null);
  if (!repoDir) return showFormError("Escolha a pasta do repositório.");
  const agentId = ($("#agent") as HTMLSelectElement).value;
  const task = slug(($("#task") as HTMLInputElement).value) || undefined;
  const btn = $<HTMLButtonElement>("#start");
  btn.disabled = true;
  try {
    const res = (await api.start(agentId, repoDir, task)) as {
      key: string; label: string; cwd: string; branch?: string; modes: SessionModeState | null;
    };
    const s: Sess = {
      key: res.key, label: res.label, cwd: res.cwd, branch: res.branch, modes: res.modes, timeline: emptyTimeline(),
      status: "iniciando", permEntry: new Map(), permOptions: new Map(), rendered: new Map(),
    };
    // eventos podem ter chegado antes do retorno do start (ready): reaplica o que ficou na fila
    for (const ev of early.get(res.key) ?? []) apply(s, ev);
    early.delete(res.key);
    sessions.set(s.key, s);
    ($("#task") as HTMLInputElement).value = "";
    select(s.key);
  } catch (err) {
    showFormError(err instanceof Error ? err.message.replace(/^Error invoking remote method '[^']+': /, "") : String(err));
  } finally {
    btn.disabled = false;
  }
}

// ---- eventos do agente ---------------------------------------------------------------
const early = new Map<string, HostEvent[]>();

function onEvent({ key, event }: { key: string; event: HostEvent }) {
  const s = sessions.get(key);
  if (!s) { early.set(key, [...(early.get(key) ?? []), event]); return; }
  apply(s, event);
  renderList();
  if (key === active) renderView();
}

function apply(s: Sess, ev: HostEvent) {
  switch (ev.type) {
    case "ready":
      s.status = "pronto";
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

// ---- ações ----------------------------------------------------------------------------
async function sendPrompt() {
  const s = active ? sessions.get(active) : null;
  const box = $<HTMLTextAreaElement>("#prompt");
  const text = box.value.trim();
  if (!s || !text || s.status === "encerrado") return;
  s.timeline = addEntry(s.timeline, { kind: "user", text });
  s.status = "trabalhando";
  box.value = "";
  renderView();
  renderList();
  try {
    await api.prompt(s.key, text);
  } catch (err) {
    s.timeline = addEntry(s.timeline, { kind: "notice", text: `erro: ${String(err)}` });
    s.status = "pronto";
    renderView();
  }
}

function modeName(s: Sess, id: string): string {
  return s.modes?.availableModes.find((m) => m.id === id)?.name ?? id;
}

async function onModeChange() {
  const s = active ? sessions.get(active) : null;
  const sel = $<HTMLSelectElement>("#mode");
  if (!s) return;
  try {
    await api.setMode(s.key, sel.value);
  } catch (err) {
    s.timeline = addEntry(s.timeline, { kind: "notice", text: `erro ao trocar o modo: ${String(err)}` });
  }
  renderView();   // em erro, o select volta ao modo que o agente confirmou
}

async function closeActive() {
  if (!active) return;
  const key = active;
  await api.close(key).catch(() => undefined);
  sessions.delete(key);
  active = sessions.keys().next().value ?? null;
  renderList();
  renderView();
}

function select(key: string) {
  active = key;
  const s = sessions.get(key)!;
  s.rendered.clear();
  $("#timeline").replaceChildren();
  renderList();
  renderView();
}

// ---- desenho --------------------------------------------------------------------------
function renderList() {
  const ul = $("#sessions");
  ul.replaceChildren();
  for (const s of sessions.values()) {
    const li = el("li", s.key === active ? "active" : "");
    li.append(el("strong", "", s.label), el("br"), el("span", "mono muted", s.branch ?? s.cwd));
    if (s.status === "aguardando você") li.prepend(el("span", "badge", "!"));
    li.addEventListener("click", () => select(s.key));
    ul.append(li);
  }
}

const statusClass: Record<Status, string> = {
  "iniciando": "working", "pronto": "", "trabalhando": "working", "aguardando você": "waiting", "encerrado": "dead",
};

function renderView() {
  const s = active ? sessions.get(active) : null;
  $("#empty").hidden = !!s;
  $("#view").hidden = !s;
  if (!s) return;
  $("#view-title").textContent = s.label;
  const st = $("#view-status");
  st.textContent = s.status;
  st.className = `pill ${statusClass[s.status]}`;
  $("#view-cwd").textContent = s.branch ? `${s.cwd} · ${s.branch}` : s.cwd;
  $<HTMLButtonElement>("#send").disabled = s.status !== "pronto";
  renderModes(s);
  $<HTMLButtonElement>("#cancel").disabled = s.status === "pronto" || s.status === "encerrado";
  renderPlan(s);
  renderTimeline(s);
}

function renderModes(s: Sess) {
  const box = $("#mode-box");
  const modes = s.modes?.availableModes ?? [];
  box.hidden = modes.length === 0;
  const sel = $<HTMLSelectElement>("#mode");
  sel.replaceChildren(...modes.map((m) => {
    const o = new Option(m.name, m.id);
    o.title = m.description ?? "";
    return o;
  }));
  sel.value = s.modes?.currentModeId ?? "";
  sel.disabled = s.status === "encerrado";
}

function renderPlan(s: Sess) {
  const box = $("#plan-box");
  box.hidden = s.timeline.plan.length === 0;
  const ol = $("#plan");
  ol.replaceChildren(...s.timeline.plan.map((p) => el("li", p.status, p.content)));
}

function renderTimeline(s: Sess) {
  const root = $("#timeline");
  const nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 80;
  for (const entry of s.timeline.entries) {
    const prev = s.rendered.get(entry.id);
    if (prev && prev.entry === entry) continue;          // timeline é imutável: mesmo objeto = sem mudança
    const node = renderEntry(s, entry);
    if (prev) prev.el.replaceWith(node);
    else root.append(node);
    s.rendered.set(entry.id, { entry, el: node });
  }
  if (nearBottom) root.scrollTop = root.scrollHeight;
}

function renderEntry(s: Sess, e: Entry): HTMLElement {
  const box = el("div", `entry ${e.kind}`);
  switch (e.kind) {
    case "user":
    case "agent":
    case "thought":
      box.textContent = e.text;
      break;
    case "tool": {
      const head = el("div", "head");
      head.append(el("strong", "", e.title), el("span", `status ${e.status}`, e.status));
      if (e.toolKind) head.append(el("span", "pill", e.toolKind));
      box.append(head);
      if (e.locations.length) box.append(el("div", "mono muted", e.locations.join("  ")));
      for (const d of e.diffs) box.append(renderDiff(d));
      if (e.output) box.append(el("pre", "mono", e.output.slice(-6000)));
      break;
    }
    case "permission": {
      box.append(el("strong", "", "Pedido de permissão: "), el("span", "", e.title));
      const requestId = [...s.permEntry.entries()].find(([, id]) => id === e.id)?.[0];
      const options = requestId ? s.permOptions.get(requestId) : undefined;
      if (e.answer) {
        box.append(el("div", "muted", `respondido: ${e.answer}`));
      } else if (requestId && options) {
        const row = el("div", "row");
        for (const o of options) {
          const b = el("button", o.kind.startsWith("reject") ? "ghost" : "", o.name);
          b.title = o.kind;
          b.addEventListener("click", () => api.answer(s.key, requestId, o.optionId));
          row.append(b);
        }
        box.append(row);
      }
      break;
    }
    case "stop":
      box.textContent = `fim do turno: ${e.reason}`;
      break;
    case "notice":
      box.textContent = e.text;
      break;
  }
  return box;
}

function renderDiff(d: { path: string; oldText?: string | null; newText: string }): HTMLElement {
  const pre = el("pre", "mono diff");
  pre.append(el("div", "muted", d.path));
  const oldLines = d.oldText ? d.oldText.split("\n") : [];
  const newLines = d.newText.split("\n");
  const oldSet = new Set(oldLines);
  const newSet = new Set(newLines);
  for (const l of oldLines) if (!newSet.has(l)) pre.append(el("span", "del", `- ${l}`));
  for (const l of newLines) if (!oldSet.has(l)) pre.append(el("span", "add", `+ ${l}`));
  return pre;
}

init();
