import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MarkdownView, readableInlineMath } from "./MarkdownView";

describe("MarkdownView chemistry notation", () => {
  afterEach(() => cleanup());

  it("renders common MinerU inline chemistry as readable text", () => {
    render(
      <MarkdownView content={String.raw`$Pd_{2}(dba)_{3}\cdot CHCl_{3}$ , $(S)-(-)$ -MeO-MOP, $-78^{\circ}C$`} />,
    );

    expect(screen.getByText(/Pd₂\(dba\)₃·CHCl₃/)).toBeInTheDocument();
    expect(document.body).toHaveTextContent("(S)-(−)-MeO-MOP");
    expect(document.body).toHaveTextContent("−78 °C");
    expect(document.body).not.toHaveTextContent("Pd_{2}");
  });

  it("keeps unsupported commands visible instead of throwing", () => {
    expect(readableInlineMath(String.raw`\unknown{CH}_{3}`)).toBe("unknownCH₃");
  });
});
