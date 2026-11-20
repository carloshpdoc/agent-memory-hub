import { describe, expect, it } from "vitest";
import { facets, filterSessions, NONE, parseDetail, parseList, parsePack, toolLabel, validSessionId } from "../src/history";

const row = (o: Record<string, unknown> = {}) => ({
  session_id: "11111111-aaaa", started_at: "2026-11-20T10:00:00Z", machine: "mac-a", tool: "claude-code",
  project: "hub", summary: "corrige o login", ...o,
});

describe("histórico do hub", () => {
  it("lê a lista e conta as linhas sem id válido em vez de sumir com elas", () => {
    const { sessions, dropped } = parseList({ sessions: [row(), row({ session_id: "x%" }), null, row({ session_id: "22222222-bbbb", project: null })] });
    expect(sessions.map((s) => s.sessionId)).toEqual(["11111111-aaaa", "22222222-bbbb"]);
    expect(sessions[1].project).toBe(NONE);
    expect(dropped).toBe(2);
  });

  it("erro do script vira exceção com a mensagem dele", () => {
    expect(() => parseList({ error: "banco fora do ar" })).toThrow("banco fora do ar");
    expect(() => parseList([])).toThrow("fora do formato");
    expect(() => parsePack({ pack: "" })).toThrow("vazio");
  });

  it("detalhe guarda só turnos bem formados", () => {
    const d = parseDetail({ ...row(), turns: [{ role: "user", text: "oi" }, { role: "system", text: "x" }, { role: "agent", text: "olá" }] });
    expect(d.turns).toEqual([{ role: "user", text: "oi" }, { role: "agent", text: "olá" }]);
  });

  it("filtra por máquina, ferramenta, projeto e texto", () => {
    const { sessions } = parseList({ sessions: [
      row(), row({ session_id: "22222222-bbbb", machine: "mac-b", tool: "codex", summary: "deploy" }),
      row({ session_id: "33333333-cccc", project: "app", summary: "LOGIN no app" }),
    ] });
    expect(facets(sessions)).toEqual({ machines: ["mac-a", "mac-b"], tools: ["claude-code", "codex"], projects: ["app", "hub"] });
    expect(filterSessions(sessions, { machine: "mac-b" }).map((s) => s.sessionId)).toEqual(["22222222-bbbb"]);
    expect(filterSessions(sessions, { text: "login" })).toHaveLength(2);
    expect(filterSessions(sessions, { text: "login", project: "app" }).map((s) => s.sessionId)).toEqual(["33333333-cccc"]);
    expect(filterSessions(sessions, {})).toHaveLength(3);
  });

  it("valida id e rotula ferramenta", () => {
    expect(validSessionId("625f91eb-0f1d")).toBe(true);
    expect(validSessionId("abc%")).toBe(false);
    expect(validSessionId(undefined)).toBe(false);
    expect(toolLabel("claude-code")).toBe("Claude Code");
    expect(toolLabel("outra")).toBe("outra");
  });
});
