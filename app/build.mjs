// Empacota main/preload (Node, CommonJS) e a tela (browser) em dist/.
import { build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";

mkdirSync("dist", { recursive: true });
const common = { bundle: true, sourcemap: true, logLevel: "warning" };
await build({ ...common, entryPoints: ["src/main.ts"], outfile: "dist/main.js", platform: "node", format: "cjs",
  target: "node22", external: ["electron"] });
await build({ ...common, entryPoints: ["src/preload.ts"], outfile: "dist/preload.js", platform: "node", format: "cjs",
  target: "node22", external: ["electron"] });
await build({ ...common, entryPoints: ["src/renderer/renderer.ts"], outfile: "dist/renderer.js", platform: "browser",
  format: "iife", target: "chrome130" });
await build({ ...common, entryPoints: ["src/smoke.ts"], outfile: "dist/smoke.js", platform: "node", format: "cjs",
  target: "node22" });
for (const f of ["index.html", "style.css"]) copyFileSync(`src/renderer/${f}`, `dist/${f}`);
console.log("build ok");
