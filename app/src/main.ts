/**
 * Processo principal do Electron: janela, sessões de agentes e a ponte IPC com a tela.
 * A tela (renderer) não tem Node: só chama os canais abaixo, que validam tudo que recebem.
 */
import { app, BrowserWindow, dialog, ipcMain, type IpcMainInvokeEvent } from "electron";
import path from "node:path";
import { AGENT_IDS, AGENTS, agentSpec } from "./agents";
import { AgentHost, type HostEvent } from "./agentHost";
import { readSpool, startBridge, type Bridge } from "./bridge";
import type { ExternalEvent } from "./external";
import { Partner, partnerServerEntry, type Role } from "./partner";
import { createWorktree, repoTop } from "./worktree";

interface SessionRec {
  key: string;
  agentId: string;
  label: string;
  cwd: string;
  branch?: string;
  host: AgentHost;
}

const sessions = new Map<string, SessionRec>();
/** Eventos de sessões fora do app (spool + ao vivo), os mais recentes. */
const EXTERNAL_MAX = 5000;
const external: ExternalEvent[] = [];
let externalDropped = 0;
let bridge: Bridge | null = null;
let win: BrowserWindow | null = null;
let counter = 0;

function send(key: string, event: HostEvent) {
  win?.webContents.send("session:event", { key, event });
}

function str(v: unknown, field: string): string {
  if (typeof v !== "string" || !v) throw new Error(`campo inválido: ${field}`);
  return v;
}

function session(key: unknown): SessionRec {
  const rec = sessions.get(str(key, "key"));
  if (!rec) throw new Error(`sessão não encontrada: ${String(key)}`);
  return rec;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    title: "Agent Console",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, "index.html"));
  win.on("closed", () => { win = null; });
  if (process.env.AGENT_CONSOLE_SELFTEST === "1") selfTest(win);
}

/** Carrega a tela, lê o que ela mostra e sai: 0 se a lista de agentes foi preenchida pela ponte IPC. */
function selfTest(w: BrowserWindow) {
  const errors: string[] = [];
  w.webContents.on("console-message", (e) => { if (e.level === "error") errors.push(e.message); });
  w.webContents.once("did-finish-load", async () => {
    await new Promise((r) => setTimeout(r, 500));
    const seen = await w.webContents.executeJavaScript(
      "({agents: [...document.querySelectorAll('#agent option')].map(o => o.value), hasApi: typeof window.consoleApi === 'object', hasRequire: typeof require !== 'undefined'})");
    console.log(`SELFTEST ${JSON.stringify({ ...seen, errors })}`);
    app.exit(seen.agents.length === 3 && seen.hasApi && !seen.hasRequire && errors.length === 0 ? 0 : 1);
  });
}

type Handler = (event: IpcMainInvokeEvent, arg: Record<string, unknown>) => unknown;
function handle(channel: string, fn: Handler) {
  ipcMain.handle(channel, (event, arg) => fn(event, (arg ?? {}) as Record<string, unknown>));
}

function pushExternal(e: ExternalEvent) {
  external.push(e);
  if (external.length > EXTERNAL_MAX) external.splice(0, external.length - EXTERNAL_MAX);
  win?.webContents.send("external:event", e);
}

/** Abre um agente; com o receptor no ar, a sessão ganha o servidor MCP do console (parceiro). */
async function startSession(agentId: string, cwd: string, branch: string | undefined, role: Role, label?: string) {
  const spec = agentSpec(agentId);
  const key = `s${++counter}`;
  const host = new AgentHost((event) => onHostEvent(key, event));
  const rec: SessionRec = { key, agentId: spec.id, label: label ?? spec.label, cwd, branch, host };
  sessions.set(key, rec);
  try {
    const mcp = bridge ? [partnerServerEntry(path.join(__dirname, "partnerMcp.js"), bridge, key, role)] : [];
    await host.launch(spec.launch(), cwd, mcp);
    // a config do próprio agente pode aprovar sozinha (ex. Claude em defaultMode "auto"):
    // toda sessão do app começa num modo que pede permissão, se o agente tiver um
    await host.useAskingMode();
  } catch (err) {
    host.close();
    sessions.delete(key);
    throw err;
  }
  return { key, agentId: spec.id, label: rec.label, cwd, branch, modes: host.modes };
}

function onHostEvent(key: string, event: HostEvent) {
  send(key, event);
  if (event.type === "stop") partner.onStop(key, event.reason);
  else if (event.type === "exit") partner.onGone(key, "o processo do agente encerrou");
}

const partner = new Partner({
  async open(agent, fromKey, delegationId) {
    const from = session(fromKey);
    const info = await startSession(agent, from.cwd, from.branch, "worker", `${agentSpec(agent).label} · ${delegationId}`);
    win?.webContents.send("session:opened", { ...info, parent: fromKey });
    return info.key;
  },
  label: (key) => sessions.get(key)?.label ?? key,
  cwd: (key) => session(key).cwd,
  busy: (key) => sessions.get(key)?.host.isBusy ?? false,
  prompt(key, text) {
    const rec = sessions.get(key);
    if (!rec) return;
    send(key, { type: "injected", text });
    rec.host.prompt(text).catch(() => undefined);     // o erro chega pelos eventos
  },
  notice: (key, text) => send(key, { type: "notice", text }),
});

async function startExternal() {
  const spool = await readSpool();
  external.push(...spool.events.slice(-EXTERNAL_MAX));
  externalDropped = spool.dropped;
  try {
    bridge = await startBridge(pushExternal, undefined, (c) => partner.call(c.key, c.tool, c.args));
  } catch (err) {
    console.error(`ponte de hooks não abriu: ${err instanceof Error ? err.message : String(err)}`);
  }
}

handle("external:snapshot", () => ({ events: external, dropped: externalDropped, live: bridge !== null }));

handle("agents:list", () => AGENT_IDS.map((id) => ({ id, label: AGENTS[id].label })));

handle("repo:pick", async () => {
  const res = await dialog.showOpenDialog(win!, { properties: ["openDirectory"] });
  if (res.canceled || !res.filePaths[0]) return null;
  return repoTop(res.filePaths[0]);
});

handle("session:start", async (_e, arg) => {
  const agentId = str(arg.agentId, "agentId");
  agentSpec(agentId);                                  // valida antes de criar worktree
  const repo = await repoTop(str(arg.repoDir, "repoDir"));
  let cwd = repo;
  let branch: string | undefined;
  if (arg.taskName) {
    const wt = await createWorktree(repo, str(arg.taskName, "taskName"));
    cwd = wt.path;
    branch = wt.branch;
  }
  return startSession(agentId, cwd, branch, "coordenador");
});

handle("session:setMode", (_e, arg) => session(arg.key).host.setMode(str(arg.modeId, "modeId")));

handle("session:prompt", (_e, arg) => {
  const rec = session(arg.key);
  // não espera o fim do turno: o progresso chega pelos eventos
  rec.host.prompt(str(arg.text, "text")).catch(() => undefined);
  return true;
});

handle("session:cancel", (_e, arg) => session(arg.key).host.cancel());

handle("session:answer", (_e, arg) => {
  const optionId = arg.optionId === null ? null : str(arg.optionId, "optionId");
  session(arg.key).host.answer(str(arg.requestId, "requestId"), optionId);
  return true;
});

handle("session:close", (_e, arg) => {
  const rec = session(arg.key);
  rec.host.close();
  sessions.delete(rec.key);
  partner.onGone(rec.key, "a sessão foi fechada");
  return true;
});

app.whenReady().then(async () => {
  await startExternal();
  createWindow();
});
app.on("window-all-closed", async () => {
  for (const rec of sessions.values()) rec.host.close();
  sessions.clear();
  await bridge?.close().catch(() => undefined);
  bridge = null;
  app.quit();
});
