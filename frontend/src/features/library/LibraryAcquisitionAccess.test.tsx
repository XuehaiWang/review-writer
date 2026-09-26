import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LibraryPage } from "./LibraryPage";

vi.mock("../../components/ProjectSelector", () => ({
  ProjectSelector: () => null,
  useSelectedProject: () => ({ selected: { project_id: "project-1" } }),
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows PDF access separately from relevance and only selects candidates with an open source", async () => {
  const posts: Array<{ candidates: Array<{ candidate_id: string }> }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const path = String(input);
    if (path.includes("/search-jobs/current")) return Response.json({ job: {
      id: "search-1", status: "succeeded", created_at: "2026-09-25T00:00:00Z",
      result: { candidates: [
        { candidate_id: "a", title: "Open paper", score: 0.02, availability: { state: "available", source_found: true } },
        { candidate_id: "b", title: "Restricted paper", score: 0.9, availability: { state: "not_found", source_found: false } },
      ] },
    } });
    if (path.includes("/download-jobs/current")) return Response.json({ job: null });
    if (path.includes("/download-jobs") && init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return Response.json({ id: "download-1", status: "queued", result: {} });
    }
    if (path.includes("/upload-jobs/recent")) return Response.json({ items: [], batch_summaries: [] });
    return Response.json({ items: [], count: 0 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><LibraryPage /></MemoryRouter></QueryClientProvider>);
  expect(await screen.findByText("Open paper")).toBeInTheDocument();
  expect(screen.getByText("相关度 2%")).toBeInTheDocument();
  expect(screen.getByText("开放 PDF 文件头已验证 · 可尝试下载")).toBeInTheDocument();
  expect(screen.getByText("暂未找到开放 PDF · 可能需要机构权限")).toBeInTheDocument();
  const openRow = screen.getByText("Open paper").closest(".candidate-row")!;
  const restrictedRow = screen.getByText("Restricted paper").closest(".candidate-row")!;
  expect(restrictedRow.querySelector("input[type=checkbox]")).toBeDisabled();
  fireEvent.click(openRow.querySelector("input[type=checkbox]")!);
  fireEvent.click(screen.getByRole("button", { name: "下载所选 1 篇" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].candidates.map((row) => row.candidate_id)).toEqual(["a"]);
  client.clear();
});
