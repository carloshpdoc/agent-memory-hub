/** Executa scripts/app_history.py para a aba Histórico (só no processo principal). */
import { execFile } from "node:child_process";
import path from "node:path";

const TIMEOUT_MS = 30_000;
const MAX_BUFFER = 64 * 1024 * 1024;
/** dist/main.js -> app/dist -> raiz do repositório do hub. */
export const HISTORY_SCRIPT = path.resolve(__dirname, "..", "..", "scripts", "app_history.py");

/** Roda o script e devolve o JSON lido; o erro que o script imprimiu vira a mensagem. */
export function runHistory(args: string[]): Promise<unknown> {
  const python = process.env.AMH_PYTHON || "python3";
  return new Promise((resolve, reject) => {
    execFile(python, [HISTORY_SCRIPT, ...args], { timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER }, (err, stdout, stderr) => {
      let parsed: unknown;
      try { parsed = JSON.parse(stdout); } catch { parsed = undefined; }
      if (parsed !== undefined) return resolve(parsed);          // {error} também é JSON: o parse* trata
      const why = err ? (err.killed ? `sem resposta em ${TIMEOUT_MS / 1000}s` : err.message) : "saída não é JSON";
      reject(new Error(`histórico do hub: ${why}${stderr.trim() ? `: ${stderr.trim().slice(-500)}` : ""}`));
    });
  });
}
