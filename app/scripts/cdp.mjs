#!/usr/bin/env node
/**
 * Vê e mexe na janela do Agent Console aberto com AGENT_CONSOLE_CDP_PORT (porta de
 * depuração do Chromium, só em 127.0.0.1). Sem dependências: fetch + WebSocket do Node.
 *
 *   node app/scripts/cdp.mjs shot <arquivo.png>     screenshot da janela
 *   node app/scripts/cdp.mjs text                   texto visível da janela
 *   node app/scripts/cdp.mjs eval "<expressão js>"  avalia na tela e imprime o resultado (JSON)
 *   node app/scripts/cdp.mjs click "<seletor css>"  clica no primeiro elemento do seletor
 */
import { writeFileSync } from "node:fs";

const PORT = process.env.AGENT_CONSOLE_CDP_PORT ?? "9222";
const TIMEOUT_MS = 10_000;
const USAGE = "uso: cdp.mjs shot <arquivo.png> | text | eval <js> | click <seletor>";

async function page() {
  let list;
  try {
    list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  } catch (e) {
    throw new Error(`porta ${PORT} não responde: abra o app com AGENT_CONSOLE_CDP_PORT=${PORT} (${e.message})`);
  }
  const p = list.find((t) => t.type === "page" && t.url.endsWith("/index.html"));
  if (!p) throw new Error(`nenhuma janela do Agent Console na porta ${PORT}`);
  return p.webSocketDebuggerUrl;
}

async function session(url) {
  const ws = new WebSocket(url);
  await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = () => bad(new Error("websocket não abriu")); });
  let seq = 0;
  const waiting = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    const w = waiting.get(msg.id);
    if (!w) return;
    waiting.delete(msg.id);
    msg.error ? w.bad(new Error(msg.error.message)) : w.ok(msg.result);
  };
  const send = (method, params = {}) => new Promise((ok, bad) => {
    const id = ++seq;
    waiting.set(id, { ok, bad });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (waiting.delete(id)) bad(new Error(`${method}: sem resposta em ${TIMEOUT_MS} ms`)); }, TIMEOUT_MS);
  });
  return { send, close: () => ws.close() };
}

async function evaluate(s, expression) {
  const r = await s.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
  return r.result.value;
}

async function main([cmd, arg]) {
  if (!cmd || (cmd !== "text" && !arg)) { console.error(USAGE); return 2; }
  const s = await session(await page());
  try {
    switch (cmd) {
      case "shot": {
        const { data } = await s.send("Page.captureScreenshot", { format: "png" });
        writeFileSync(arg, Buffer.from(data, "base64"));
        console.log(arg);
        break;
      }
      case "text": console.log(await evaluate(s, "document.body.innerText")); break;
      case "eval": console.log(JSON.stringify(await evaluate(s, arg), null, 2)); break;
      case "click": {
        const ok = await evaluate(s, `(() => { const el = document.querySelector(${JSON.stringify(arg)}); if (!el) return false; el.click(); return true; })()`);
        if (!ok) throw new Error(`nenhum elemento para ${arg}`);
        console.log(`clicado: ${arg}`);
        break;
      }
      default: console.error(USAGE); return 2;
    }
  } finally {
    s.close();
  }
  return 0;
}

main(process.argv.slice(2)).then((c) => process.exit(c), (e) => { console.error(e.message); process.exit(1); });
