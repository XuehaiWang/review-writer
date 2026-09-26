import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { FulltextAccess, articleSourceUrl, fulltextLabel, fulltextHint } from "./FulltextAccess";

vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ text: (zh: string) => zh }) }));
const text = (zh: string) => zh;
afterEach(cleanup);

describe("full-text access", () => {
  it("shows verified availability before any download and offers the checked PDF", () => {
    const row = { availability: { state: "available" as const, pdf_url: "https://example.org/verified.pdf" } };
    expect(fulltextLabel(row, text)).toContain("可下载");
    render(<FulltextAccess row={row} libraryUrl="/library" onDownload={vi.fn()} />);
    expect(screen.getByRole("link", { name: "下载 PDF" })).toHaveAttribute("href", row.availability.pdf_url);
    expect(fulltextLabel({ availability: { state: "unknown" } }, text)).toContain("无法确认");
    expect(fulltextLabel({ availability: { state: "restricted" } }, text)).toBe("来源限制访问");
  });
  it("does not call an OA flag a successful download", () => {
    expect(fulltextLabel({ access_status: "open_access_downloadable" }, text)).toContain("线索");
    expect(fulltextLabel({ fulltext_access: { state: "downloaded", reason: "" } }, text)).toContain("解析状态");
  });
  it("uses DOI when the link is unsafe and rejects unsupported links", () => {
    expect(articleSourceUrl({ landing_url: "javascript:alert(1)", doi: "10.1234/example" })).toBe("https://doi.org/10.1234/example");
    expect(articleSourceUrl({ landing_url: "https://user:secret@example.com/" })).toBe("");
    expect(articleSourceUrl({ landing_url: "file:///etc/passwd" })).toBe("");
  });
  it("offers acquisition, source and upload without selecting the paper", () => {
    const download = vi.fn();
    render(<FulltextAccess row={{ doi: "10.1234/example" }} libraryUrl="/library" onDownload={download} />);
    fireEvent.click(screen.getByRole("button", { name: "查找开放全文" }));
    expect(download).toHaveBeenCalledOnce();
    expect(screen.getByRole("link", { name: "打开原文" }).getAttribute("href")).toContain("doi.org");
    expect(screen.getByRole("link", { name: "上传 PDF" })).toBeInTheDocument();
  });
  it("shows actionable failure and disables repeated active requests", () => {
    expect(fulltextHint({ fulltext_access: { state: "not_acquired", reason: "rate_limited" } }, text)).toContain("限流");
    render(<FulltextAccess row={{ doi: "10.1234/example" }} processing libraryUrl="/library" onDownload={vi.fn()} />);
    expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
  });
});
