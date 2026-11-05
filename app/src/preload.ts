/** Única porta entre a tela e o processo principal: canais fixos, sem Node na tela. */
import { contextBridge, ipcRenderer } from "electron";

const api = {
  listAgents: () => ipcRenderer.invoke("agents:list"),
  pickRepo: () => ipcRenderer.invoke("repo:pick"),
  start: (agentId: string, repoDir: string, taskName?: string) =>
    ipcRenderer.invoke("session:start", { agentId, repoDir, taskName }),
  prompt: (key: string, text: string) => ipcRenderer.invoke("session:prompt", { key, text }),
  setMode: (key: string, modeId: string) => ipcRenderer.invoke("session:setMode", { key, modeId }),
  cancel: (key: string) => ipcRenderer.invoke("session:cancel", { key }),
  answer: (key: string, requestId: string, optionId: string | null) =>
    ipcRenderer.invoke("session:answer", { key, requestId, optionId }),
  close: (key: string) => ipcRenderer.invoke("session:close", { key }),
  externalSnapshot: () => ipcRenderer.invoke("external:snapshot"),
  onExternal: (cb: (e: unknown) => void) => {
    const listener = (_e: unknown, e: unknown) => cb(e);
    ipcRenderer.on("external:event", listener);
    return () => ipcRenderer.removeListener("external:event", listener);
  },
  /** Sessão aberta pelo app (worker de uma delegação), não pela tela. */
  onOpened: (cb: (info: unknown) => void) => {
    const listener = (_e: unknown, info: unknown) => cb(info);
    ipcRenderer.on("session:opened", listener);
    return () => ipcRenderer.removeListener("session:opened", listener);
  },
  onEvent: (cb: (msg: unknown) => void) => {
    const listener = (_e: unknown, msg: unknown) => cb(msg);
    ipcRenderer.on("session:event", listener);
    return () => ipcRenderer.removeListener("session:event", listener);
  },
};

contextBridge.exposeInMainWorld("consoleApi", api);
export type ConsoleApi = typeof api;
