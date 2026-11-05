/**
 * Tela do console. Todo texto vindo do agente é dado não confiável: entra no DOM só por
 * textContent, nunca como HTML.
 */
import type { PermissionOption, SessionModeState } from "@agentclientprotocol/sdk";
import type { HostEvent } from "../agentHost";
import type { ConsoleApi } from "../preload";
import { buildHandoff, NEXT_STEP, splitHandoff } from "../handoff";
import { parseMarkdown, type Inline } from "../markdown";
import { foldExternal, type ExternalEvent, type ExternalSession } from "../external";
import { addEntry, answerPermission, applyUpdate, emptyTimeline, type Entry, type Timeline } from "../timeline";

declare global {
  interface Window { consoleApi: ConsoleApi }
}
const api = window.consoleApi;

type Status = "iniciando" | "pronto" | "trabalhando" | "aguardando você" | "encerrado";
interface Sess {
  key: string;
  agentId: string;
  label: string;
  sessionId?: string;     // id da sessão no agente (aponta para o histórico no hub)
  draft: string;          // texto não enviado da caixa de mensagem desta sessão
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
  $("#continue").addEventListener("click", continueIn);
  for (const a of agents) $("#continue-agent").append(new Option(a.label, a.id));
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
    li.title = [s.sessionId, s.cwd, s.lastDetail].filter(Boolean).join("\n");   // detalhe inteiro no hover
    const where = s.cwd.split("/").filter(Boolean).pop() ?? s.cwd;
    li.append(
      el("strong", "", `${s.tool === "claude" ? "Claude" : "Codex"} · ${where}`), el("br"),
      el("span", "mono muted", `${s.lastEvent}${s.lastDetail ? `: ${s.lastDetail}` : ""}`),
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
    const s = await openSession(agentId, repoDir, task);
    ($("#task") as HTMLInputElement).value = "";
    select(s.key);
  } catch (err) {
    showFormError(errText(err));
  } finally {
    btn.disabled = false;
  }
}

const errText = (err: unknown) =>
  err instanceof Error ? err.message.replace(/^Error invoking remote method '[^']+': /, "") : String(err);

async function openSession(agentId: string, dir: string, task?: string): Promise<Sess> {
  const res = (await api.start(agentId, dir, task)) as {
    key: string; agentId: string; label: string; cwd: string; branch?: string; modes: SessionModeState | null;
  };
  const s: Sess = {
    key: res.key, agentId: res.agentId, label: res.label, cwd: res.cwd, branch: res.branch, modes: res.modes,
    draft: "", timeline: emptyTimeline(),
    status: "iniciando", permEntry: new Map(), permOptions: new Map(), rendered: new Map(),
  };
  // eventos podem ter chegado antes do retorno do start (ready): reaplica o que ficou na fila
  for (const ev of early.get(res.key) ?? []) apply(s, ev);
  early.delete(res.key);
  sessions.set(s.key, s);
  return s;
}

/**
 * Abre o agente escolhido na mesma pasta e põe o pacote de continuação na caixa de mensagem:
 * o usuário revisa, escreve o próximo passo e envia. A sessão de origem fica aberta.
 */
async function continueIn() {
  const from = active ? sessions.get(active) : null;
  const sel = $<HTMLSelectElement>("#continue-agent");
  if (!from || !sel.value) return;
  const btn = $<HTMLButtonElement>("#continue");
  btn.disabled = true;
  try {
    const to = await openSession(sel.value, from.cwd);
    to.branch = from.branch;
    const pack = buildHandoff({
      timeline: from.timeline, fromLabel: from.label, toLabel: to.label, cwd: from.cwd, branch: from.branch,
      sessionId: from.sessionId, interrupted: from.status === "trabalhando" || from.status === "aguardando você",
    });
    to.draft = `${pack}\n\n${NEXT_STEP}\n`;
    to.timeline = addEntry(to.timeline, { kind: "notice", text: `continuação de ${from.label}: revise o contexto na caixa abaixo, escreva o próximo passo e envie` });
    from.timeline = addEntry(from.timeline, { kind: "notice", text: `continuada em ${to.label}` });
    select(to.key);
  } catch (err) {
    from.timeline = addEntry(from.timeline, { kind: "notice", text: `não abriu ${sel.selectedOptions[0]?.text ?? sel.value}: ${errText(err)}` });
    renderView();
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
      s.sessionId = ev.sessionId;
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
/** Aviso que não se repete a cada clique: só entra se não for o último da linha do tempo. */
function notifyOnce(s: Sess, text: string) {
  const last = s.timeline.entries[s.timeline.entries.length - 1];
  if (last?.kind === "notice" && last.text === text) return;
  s.timeline = addEntry(s.timeline, { kind: "notice", text });
}

async function sendPrompt() {
  const s = active ? sessions.get(active) : null;
  const box = $<HTMLTextAreaElement>("#prompt");
  const text = box.value.trim();
  if (!s || !text || s.status === "encerrado") return;
  if (text.endsWith(NEXT_STEP)) {                          // pacote sem pedido: o agente só perguntaria
    notifyOnce(s, `escreva o próximo passo embaixo de "${NEXT_STEP}" antes de enviar`);
    renderView();
    return;
  }
  s.timeline = addEntry(s.timeline, { kind: "user", text });
  s.status = "trabalhando";
  box.value = "";
  s.draft = "";
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
  const box = $<HTMLTextAreaElement>("#prompt");
  const prev = active ? sessions.get(active) : null;
  if (prev) prev.draft = box.value;
  active = key;
  const s = sessions.get(key)!;
  box.value = s.draft;
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
  renderContinue(s);
  $<HTMLButtonElement>("#cancel").disabled = s.status === "pronto" || s.status === "encerrado";
  renderPlan(s);
  renderTimeline(s);
}

/** Continuar no mesmo agente não troca nada: o próprio agente sai da lista. */
function renderContinue(s: Sess) {
  const sel = $<HTMLSelectElement>("#continue-agent");
  for (const o of sel.options) o.disabled = o.value === s.agentId;
  if (sel.selectedOptions[0]?.disabled) sel.value = [...sel.options].find((o) => !o.disabled)?.value ?? "";
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

const LONG_USER = 2000;

function renderEntry(s: Sess, e: Entry): HTMLElement {
  const box = el("div", `entry ${e.kind}`);
  switch (e.kind) {
    case "agent":
    case "thought":                                        // o Codex manda pensamentos com **título**
      box.classList.add("md");
      box.append(...renderMarkdown(e.text));
      break;
    case "user": {
      const split = splitHandoff(e.text);
      if (split) {                                         // pacote recolhido, o pedido à vista
        const d = el("details", "md");
        d.append(el("summary", "", `contexto da continuação (${split.pack.length} caracteres)`), ...renderMarkdown(split.pack));
        box.append(d);
        if (split.next) box.append(el("div", "next", split.next));
      } else if (e.text.length > LONG_USER) {                     // pacote de continuação: recolhido
        const d = el("details");
        d.append(el("summary", "", `${e.text.split("\n")[0].replace(/^#+\s*/, "")} (${e.text.length} caracteres)`),
          el("div", "", e.text));
        box.append(d);
      } else box.textContent = e.text;
      break;
    }
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

function renderInline(parts: Inline[]): Node[] {
  return parts.map((p) => (p.t === "text" ? document.createTextNode(p.v) : el(p.t === "bold" ? "strong" : "code", "", p.v)));
}

function renderMarkdown(text: string): HTMLElement[] {
  return parseMarkdown(text).map((b) => {
    switch (b.kind) {
      case "heading": { const h = el("h4"); h.append(...renderInline(b.inline)); return h; }
      case "para": { const p = el("p"); p.append(...renderInline(b.inline)); return p; }
      case "code": return el("pre", "mono", b.text);
      case "list": {
        const l = el(b.ordered ? "ol" : "ul");
        for (const item of b.items) { const li = el("li"); li.append(...renderInline(item)); l.append(li); }
        return l;
      }
    }
  });
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
