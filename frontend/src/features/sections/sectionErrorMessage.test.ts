import { describe, expect, it } from "vitest";
import { sectionErrorMessage } from "./sectionErrorMessage";
const zh = (value: string) => value;
describe("section error summaries", () => {
  it("summarizes a wrapped provider failure without exposing internal retry details", () => {
    expect(sectionErrorMessage("Scientific provider became unavailable during task attempt 1 after internal retries. Earlier model calls completed. Section generation completed 5 section(s), but 2 failed: S03, S07.", zh))
      .toBe("模型服务暂时不可用，请稍后继续生成。");
  });
  it("distinguishes a dependent conclusion from a service failure", () => {
    expect(sectionErrorMessage("Conclusion deferred until incomplete body sections are repaired.", zh)).toBe("等待正文完成后生成总结。");
  });
  it("keeps quota failures actionable and bounds unknown diagnostics", () => {
    expect(sectionErrorMessage("Scientific task failed: insufficient credit", zh)).toContain("联系管理员");
    expect(sectionErrorMessage("Traceback " + "x".repeat(2000), zh).length).toBeLessThan(100);
  });
});
