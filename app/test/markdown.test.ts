import { describe, expect, it } from "vitest";
import { parseInline, parseMarkdown } from "../src/markdown";

describe("markdown das falas", () => {
  it("negrito e código no meio do texto; o resto fica texto", () => {
    expect(parseInline("usa **SharedModels** em `App` e <b>x</b>")).toEqual([
      { t: "text", v: "usa " }, { t: "bold", v: "SharedModels" }, { t: "text", v: " em " },
      { t: "code", v: "App" }, { t: "text", v: " e <b>x</b>" },
    ]);
  });

  it("parágrafos, títulos, listas numeradas e com traço, e bloco de código", () => {
    const b = parseMarkdown("**Testes**\n- um\n- dois\n\n## Fim\n1. a\n\n2. b\n```\nlet x = 1\n```\ntexto\nlinha 2");
    expect(b.map((x) => x.kind)).toEqual(["para", "list", "heading", "list", "code", "para"]);
    expect(b[1]).toMatchObject({ ordered: false, items: [[{ v: "um" }], [{ v: "dois" }]] });
    expect(b[3]).toMatchObject({ ordered: true });
    expect((b[3] as { items: unknown[] }).items).toHaveLength(2);       // linha vazia não quebra a lista
    expect(b[4]).toEqual({ kind: "code", text: "let x = 1" });
    expect(b[5]).toEqual({ kind: "para", inline: [{ t: "text", v: "texto\nlinha 2" }] });
  });

  it("cerca de código sem fechamento engole até o fim, sem perder texto", () => {
    expect(parseMarkdown("a\n```\nb\nc")).toEqual([
      { kind: "para", inline: [{ t: "text", v: "a" }] }, { kind: "code", text: "b\nc" },
    ]);
  });
});
