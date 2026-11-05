/**
 * Receptor da ponte, com o hook Python real (hooks/console_bridge.py) falando com ele:
 * o mesmo caminho que um Claude ou Codex no terminal usa.
 */
import { spawn } from "node:child_process";
import { promises as fs, mkdtempSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { readSpool, startBridge, TOKEN_HEADER, type Bridge } from "../src/bridge";
import { foldExternal, parseExternal, type ExternalEvent, type ExternalSession } from "../src/external";

const HOOK = path.resolve(__dirname, "../../hooks/console_bridge.py");
const PAYLOAD = { hook_event_name: "PostToolUse", session_id: "S1", cwd: "/r", tool_name: "Bash", tool_input: { command: "ls" } };

let bridge: Bridge | null = null;
afterEach(async () => { await bridge?.close(); bridge = null; });

const tmpHome = () => mkdtempSync(path.join(os.tmpdir(), "console-"));

function runHook(home: string, payload: unknown, tool = "claude", extraEnv: Record<string, string> = {}) {
  const env: NodeJS.ProcessEnv = { ...process.env, AGENT_CONSOLE_HOME: home, ...extraEnv };
  if (!("AGENT_CONSOLE_SESSION" in extraEnv)) delete env.AGENT_CONSOLE_SESSION;
  // assíncrono: o receptor roda neste mesmo processo e precisa responder enquanto o hook espera
  return new Promise<string>((resolve, reject) => {
    const child = spawn("python3", [HOOK, "--tool", tool], { env });
    let out = "";
    child.stdout.on("data", (b: Buffer) => { out += b.toString(); });
    child.on("error", reject);
    child.on("exit", (code) => (code === 0 ? resolve(out) : reject(new Error(`hook saiu com ${code}`))));
    child.stdin.end(JSON.stringify(payload));
  });
}

const ev = (over: Partial<ExternalEvent> = {}): ExternalEvent =>
  ({ v: 1, tool: "claude", event: "PostToolUse", session_id: "S1", cwd: "/r", ts: 1, ...over });

describe("ponte de hooks", () => {
  it("o hook entrega ao app no ar, sem imprimir nada e sem tocar no spool", async () => {
    const home = tmpHome();
    const got: ExternalEvent[] = [];
    bridge = await startBridge((e) => got.push(e), home);
    expect(statSync(path.join(home, "endpoint.json")).mode & 0o777).toBe(0o600);
    expect(statSync(home).mode & 0o777).toBe(0o700);
    expect(await runHook(home, PAYLOAD, "codex")).toBe("");
    expect(got).toHaveLength(1);
    expect(got[0]).toMatchObject({ tool: "codex", event: "PostToolUse", session_id: "S1", detail: "ls" });
    expect((await readSpool(home)).files).toBe(0);
  });

  it("sessão aberta pelo console não gera evento", async () => {
    const home = tmpHome();
    const got: ExternalEvent[] = [];
    bridge = await startBridge((e) => got.push(e), home);
    await runHook(home, PAYLOAD, "claude", { AGENT_CONSOLE_SESSION: "1" });
    expect(got).toHaveLength(0);
  });

  it("com o app fechado o evento fica no spool e é lido ao abrir", async () => {
    const home = tmpHome();
    await runHook(home, PAYLOAD);
    await fs.appendFile(path.join(home, "spool.jsonl"), "linha quebrada\n");
    const r = await readSpool(home);
    expect(r.events.map((e) => e.session_id)).toEqual(["S1"]);
    expect(r.dropped).toBe(1);
  });

  it("recusa sem token, token errado, rota errada e corpo inválido", async () => {
    const home = tmpHome();
    const got: ExternalEvent[] = [];
    bridge = await startBridge((e) => got.push(e), home);
    const { token } = JSON.parse(await fs.readFile(path.join(home, "endpoint.json"), "utf8"));
    const post = (p: string, headers: Record<string, string>, body: string) =>
      fetch(`http://127.0.0.1:${bridge!.port}${p}`, { method: "POST", headers, body }).then((r) => r.status);
    const good = JSON.stringify(ev());
    expect(await post("/hook", {}, good)).toBe(403);
    expect(await post("/hook", { [TOKEN_HEADER]: "x".repeat(token.length) }, good)).toBe(403);
    expect(await post("/outra", { [TOKEN_HEADER]: token }, good)).toBe(404);
    expect(await post("/hook", { [TOKEN_HEADER]: token }, "{}")).toBe(400);
    expect(await post("/hook", { [TOKEN_HEADER]: token }, "x".repeat(70 * 1024))).toBe(413);
    expect(await post("/hook", { [TOKEN_HEADER]: token }, good)).toBe(204);
    expect(got).toHaveLength(1);
  });

  it("fechar remove o endpoint só se ainda for deste app", async () => {
    const home = tmpHome();
    const b1 = await startBridge(() => undefined, home);
    const b2 = await startBridge(() => undefined, home);         // outro console assumiu
    await b1.close();
    expect(JSON.parse(await fs.readFile(path.join(home, "endpoint.json"), "utf8")).port).toBe(b2.port);
    await b2.close();
    await expect(fs.stat(path.join(home, "endpoint.json"))).rejects.toThrow();
  });
});

describe("sessões externas", () => {
  it("valida o formato da ponte", () => {
    expect(parseExternal(ev())).not.toBeNull();
    expect(parseExternal({ ...ev(), tool: "vim" })).toBeNull();
    expect(parseExternal({ ...ev(), session_id: "" })).toBeNull();
    expect(parseExternal({ ...ev(), ts: "1" })).toBeNull();
    expect(parseExternal([ev()])).toBeNull();
  });

  it("agrega por sessão, mantém o mais recente e marca o fim", () => {
    let m = new Map<string, ExternalSession>();
    m = foldExternal(m, ev({ ts: 10, event: "SessionStart" }));
    m = foldExternal(m, ev({ ts: 30, event: "PostToolUse", detail: "ls" }));
    m = foldExternal(m, ev({ ts: 20, event: "UserPromptSubmit" }));     // chegou atrasado
    m = foldExternal(m, ev({ tool: "codex", ts: 5 }));
    const s = m.get("claude:S1")!;
    expect([s.firstTs, s.lastTs, s.lastEvent, s.lastDetail, s.events, s.ended]).toEqual([10, 30, "PostToolUse", "ls", 3, false]);
    expect(m.size).toBe(2);
    m = foldExternal(m, ev({ ts: 40, event: "SessionEnd" }));
    expect(m.get("claude:S1")!.ended).toBe(true);
  });
});
