import { describe, expect, it } from "vitest";

import { figureTypeLabel } from "./figureTypeLabels";

describe("figureTypeLabel", () => {
  it("switches every redraw type between Chinese and English", () => {
    expect(figureTypeLabel("mechanism-cycle", "zh")).toBe("反应机理 / 催化循环");
    expect(figureTypeLabel("mechanism-cycle", "en")).toBe("Mechanism / catalytic cycle");
    expect(figureTypeLabel("colored-chemistry", "zh")).toContain("去除装饰填充");
    expect(figureTypeLabel("colored-chemistry", "en")).toContain("remove decorative fills");
  });

  it("keeps provider-defined future types readable", () => {
    expect(figureTypeLabel("future-type", "zh", "Future type")).toBe("Future type");
  });
});
