import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useUiText } from "../i18n/useUiText";
import { usePreferences } from "../state/preferences";

function LanguageProbe() {
  const { text } = useUiText();
  const setLanguage = usePreferences((state) => state.setLanguage);
  return (
    <div>
      <span>{text("正在生成", "Generating")}</span>
      <button type="button" onClick={() => setLanguage("zh-CN")}>中</button>
      <button type="button" onClick={() => setLanguage("en")}>EN</button>
    </div>
  );
}

describe("React language state", () => {
  afterEach(() => usePreferences.getState().setLanguage("zh-CN"));

  it("updates feature-local text immediately in both directions", () => {
    usePreferences.getState().setLanguage("zh-CN");
    render(<LanguageProbe />);
    expect(screen.getByText("正在生成")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(screen.getByText("Generating")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "中" }));
    expect(screen.getByText("正在生成")).toBeInTheDocument();
  });
});
