/**
 * Processo principal do Electron: janela, sessões de agentes e a ponte IPC com a tela.
 * A tela (renderer) não tem Node: só chama os canais abaixo, que validam tudo que recebem.
 */
import { app, BrowserWindow, dialog, ipcMain, type IpcMainInvokeEvent } from "electron";
import path from "node:path";
import { AGENT_IDS, AGENTS, agentSpec } from "./agents";
import { AgentHost, type HostEvent } from "./agentHost";
import { createWorktree, repoTop } from "./worktree";

interface SessionRec {
  key: string;
  agentId: string;
  cwd: string;
  branch?: string;
  host: AgentHost;
}

const sessions = new Map<string, SessionRec>();
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

handle("agents:list", () => AGENT_IDS.map((id) => ({ id, label: AGENTS[id].label })));

handle("repo:pick", async () => {
  const res = await dialog.showOpenDialog(win!, { properties: ["openDirectory"] });
  if (res.canceled || !res.filePaths[0]) return null;
  return repoTop(res.filePaths[0]);
});

handle("session:start", async (_e, arg) => {
  const spec = agentSpec(str(arg.agentId, "agentId"));
  const repo = await repoTop(str(arg.repoDir, "repoDir"));
  let cwd = repo;
  let branch: string | undefined;
  if (arg.taskName) {
    const wt = await createWorktree(repo, str(arg.taskName, "taskName"));
    cwd = wt.path;
    branch = wt.branch;
  }
  const key = `s${++counter}`;
  const host = new AgentHost((event) => send(key, event));
  sessions.set(key, { key, agentId: spec.id, cwd, branch, host });
  try {
    await host.launch(spec.launch(), cwd);
  } catch (err) {
    host.close();
    sessions.delete(key);
    throw err;
  }
  return { key, agentId: spec.id, label: spec.label, cwd, branch };
});

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
  return true;
});

app.whenReady().then(createWindow);
app.on("window-all-closed", () => {
  for (const rec of sessions.values()) rec.host.close();
  sessions.clear();
  app.quit();
});
