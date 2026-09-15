import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LibraryPage, UploadBatchProgress } from "./LibraryPage";

vi.mock("../../components/ProjectSelector", () => ({
  ProjectSelector: () => null,
  useSelectedProject: () => ({ selected: undefined }),
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("restores a completed batch older than the former twelve-second timeout", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    if (String(input).includes("/upload-jobs/recent")) return Response.json({
      items: [{ id: "old", batch_id: "batch", filename: "failed.pdf", status: "failed", error_message: "Download interrupted", updated_at: "2025-01-01T00:00:00Z" }],
      batch_summaries: [{ batch_id: "batch", total: 1, succeeded: 0, failed: 1, running: 0, queued: 0, cancelled: 0, interrupted: 0, cancel_requested: 0, updated_at: "2025-01-01T00:00:00Z" }],
    });
    return Response.json({ items: [], count: 0 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LibraryPage /></QueryClientProvider>);
  expect(await screen.findByText("批量处理已结束，部分文件失败")).toBeInTheDocument();
  fireEvent.click(screen.getByText("查看文件处理结果"));
  expect(within(screen.getByText("查看文件处理结果").closest("details")!).getByText("failed.pdf")).toBeInTheDocument();
  client.clear();
});

it("shows duplicate totals and expandable links after a reload", () => {
  const view = vi.fn();
  render(<UploadBatchProgress uploads={[{ id: "duplicate", batchId: "b", name: "renamed.pdf", status: "done", duplicate: true, paperId: "P1", message: "" }]} localUploads={[]} onViewPaper={view} summary={{
    batch_id: "b", total: 1, queued: 0, running: 0, succeeded: 1, duplicate_count: 1,
    failed: 0, cancelled: 0, interrupted: 0, cancel_requested: 0, created_at: "", updated_at: "",
  }} />);
  expect(screen.getByText("文件均已存在，已跳过重复解析")).toBeInTheDocument();
  expect(screen.getByText("重复跳过")).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  fireEvent.click(screen.getByText("查看重复文件（1）"));
  fireEvent.click(screen.getByRole("button", { name: "查看已有论文" }));
  expect(view).toHaveBeenCalledWith("P1");
});

it("uses whole batch duplicate count even when recent details are truncated", () => {
  render(<UploadBatchProgress uploads={[]} localUploads={[]} summary={{
    batch_id: "b", total: 130, queued: 0, running: 0, succeeded: 130, duplicate_count: 120,
    failed: 0, cancelled: 0, interrupted: 0, cancel_requested: 0, created_at: "", updated_at: "",
  }} />);
  expect(screen.getByText("查看重复文件（120）")).toBeInTheDocument();
  expect(screen.getByText("10")).toBeInTheDocument();
  expect(screen.queryByText("文件均已存在，已跳过重复解析")).toBeNull();
});

it("stops later uploads even when cancel is clicked while the current upload is in flight", async () => {
  let finishUpload!: (value: Response) => void;
  const submitted: string[] = [];
  const cancellations: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const path = String(input);
    if (path.includes("/upload-jobs?")) {
      submitted.push(path);
      return new Promise<Response>((resolve) => { finishUpload = resolve; });
    }
    if (path.includes("/cancel-remaining")) {
      cancellations.push(path);
      return Response.json({ cancelled_count: 0 });
    }
    if (path.includes("/upload-jobs/recent")) return Response.json({ items: [], batch_summaries: [] });
    return Response.json({ items: [], count: 0 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(<QueryClientProvider client={client}><LibraryPage /></QueryClientProvider>);
  const files = ["first.pdf", "second.pdf", "third.pdf"].map((name) => new File(["%PDF-1.4"], name, { type: "application/pdf" }));
  fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files } });
  await waitFor(() => expect(submitted).toHaveLength(1));
  fireEvent.click(screen.getByRole("button", { name: "取消剩余" }));
  await screen.findByText("已取消剩余文件，正在解析的文件将继续完成。");
  const batchId = new URL(submitted[0], "http://localhost").searchParams.get("batch_id");
  expect(cancellations).toEqual([`/api/v1/library/upload-batches/${batchId}/cancel-remaining`]);
  await act(async () => finishUpload(Response.json({ error: { message: "Batch cancelled" } }, { status: 409 })));
  await screen.findByText("批量处理已结束，剩余文件已取消");
  expect(submitted).toHaveLength(1);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  client.clear();
});

it("offers cancellation for server queues after reopening the page and keeps cancelled distinct from failure", () => {
  const cancel = vi.fn();
  render(<UploadBatchProgress uploads={[]} localUploads={[]} onCancelRemaining={cancel} summary={{
    batch_id: "old-batch", total: 5, queued: 2, running: 1, succeeded: 0, failed: 0,
    cancelled: 2, interrupted: 0, cancel_requested: 0, created_at: "", updated_at: "",
  }} />);
  fireEvent.click(screen.getByRole("button", { name: "取消剩余" }));
  expect(cancel).toHaveBeenCalledOnce();
  expect(screen.getByText("已取消")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
