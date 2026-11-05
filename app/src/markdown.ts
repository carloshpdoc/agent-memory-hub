/**
 * Markdown mínimo das falas dos agentes: títulos, parágrafos, listas, blocos de código,
 * **negrito** e `código`. Só gera uma árvore de dados; a tela monta o DOM com textContent,
 * então nada vindo do agente vira HTML.
 */
export type Inline = { t: "text" | "bold" | "code"; v: string };
export type Block =
  | { kind: "heading"; level: number; inline: Inline[] }
  | { kind: "para"; inline: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "code"; text: string };

const INLINE_RE = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
    const tok = m[0];
    out.push(tok.startsWith("`") ? { t: "code", v: tok.slice(1, -1) } : { t: "bold", v: tok.slice(2, -2) });
    last = m.index + tok.length;
  }
  if (last < text.length) out.push({ t: "text", v: text.slice(last) });
  return out;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

export function parseMarkdown(src: string): Block[] {
  const blocks: Block[] = [];
  const lines = src.split("\n");
  let para: string[] = [];
  const flush = () => {
    if (para.length) blocks.push({ kind: "para", inline: parseInline(para.join("\n")) });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trimStart().startsWith("```")) {                // bloco de código até a cerca seguinte
      flush();
      const body: string[] = [];
      for (i++; i < lines.length && !lines[i].trimStart().startsWith("```"); i++) body.push(lines[i]);
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    const h = HEADING.exec(line);
    const item = BULLET.exec(line) ?? NUMBERED.exec(line);
    if (h) {
      flush();
      blocks.push({ kind: "heading", level: h[1].length, inline: parseInline(h[2]) });
    } else if (item) {
      flush();
      const ordered = !BULLET.test(line);
      const prev = blocks[blocks.length - 1];
      if (prev?.kind === "list" && prev.ordered === ordered) prev.items.push(parseInline(item[1]));
      else blocks.push({ kind: "list", ordered, items: [parseInline(item[1])] });
    } else if (!line.trim()) {
      flush();
    } else {
      para.push(line);
    }
  }
  flush();
  return blocks;
}
