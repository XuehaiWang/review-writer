import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { usePreferences } from "../state/preferences";
import { useLocalizedMessage } from "../i18n/useLocalizedMessage";
import { LocalizedError } from "../components/LocalizedError";
import { diagnosticText } from "../i18n/diagnostics";
import { uiLocale } from "../i18n/locale";

afterEach(() => { cleanup(); usePreferences.getState().setLanguage("zh-CN"); });
it("explains translation failures without exposing a technical code", () => {
  expect(diagnosticText("QUERY_TRANSLATION_FAILED: timeout", "zh-CN")).toContain("本次检索未开始");
  expect(diagnosticText("QUERY_TRANSLATION_FAILED: timeout", "en")).toContain("Search has not started");
});
it("explains bibliography resolution failures as recoverable actions", () => {
  expect(diagnosticText("Selected bibliography candidate is unavailable or stale.", "zh-CN"))
    .toContain("重新自动核验");
  expect(diagnosticText("Bibliography record is missing required fields: title, authors.", "zh-CN"))
    .toContain("现有结果已保留");
});
function Probe() {
  const [message, setMessage] = useLocalizedMessage();
  return <><button onClick={() => setMessage(["已保存", "Saved"])}>save</button><p role="status">{message}</p></>;
}
it("translates an already displayed success without repeating its action", () => {
  usePreferences.getState().setLanguage("zh-CN");
  render(<Probe />);
  fireEvent.click(screen.getByText("save"));
  expect(screen.getByRole("status")).toHaveTextContent("已保存");
  act(() => usePreferences.getState().setLanguage("en"));
  expect(screen.getByRole("status")).toHaveTextContent("Saved");
  act(() => usePreferences.getState().setLanguage("zh-CN"));
  expect(screen.getByRole("status")).toHaveTextContent("已保存");
});
it("translates a backend figure blocker in both directions", () => {
  const message = "来源论文不可用，请先到文献库核对。";
  usePreferences.getState().setLanguage("en");
  render(<LocalizedError error={message} />);
  expect(screen.getByText("The source paper is unavailable. Check it in Library first.")).toBeVisible();
  expect(screen.queryByText(message)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Technical details" }));
  expect(screen.getByText(message)).toBeVisible();
  act(() => usePreferences.getState().setLanguage("zh-CN"));
  expect(screen.getByText(message)).toBeVisible();
});
it("keeps unfamiliar diagnostic text accessible without displaying the wrong language by default", () => {
  usePreferences.getState().setLanguage("en");
  render(<LocalizedError error={new Error("未知供应商故障：诊断42")} />);
  expect(screen.queryByText("未知供应商故障：诊断42")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Technical details" }));
  expect(screen.getByText("未知供应商故障：诊断42")).toBeVisible();
  expect(diagnosticText(new Error("余额不足"), "en")).toContain("Insufficient credit");
});
it("formats dates according to the selected UI locale", () => {
  usePreferences.getState().setLanguage("en");
  expect(uiLocale()).toBe("en-US");
  usePreferences.getState().setLanguage("zh-CN");
  expect(uiLocale()).toBe("zh-CN");
});
