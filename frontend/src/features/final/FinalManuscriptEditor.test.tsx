import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import { FinalManuscriptEditor } from "./FinalManuscriptEditor";
afterEach(cleanup);
it("keeps Final read-only with navigation to Draft and historical downloads", () => {
  const client = new QueryClient();
  render(<MemoryRouter><QueryClientProvider client={client}><FinalManuscriptEditor projectId="p" revision={1} artifactId="a" current markdown="Saved final" paragraphs={[]} versions={[{ artifact_id: "old", current: false, operation: "final-build", created_at: "yesterday" }]} refresh={async () => {}} /></QueryClientProvider></MemoryRouter>);
  expect(screen.getByText("Saved final")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("link", { name: /前往初稿|Edit in Draft/ })).toHaveAttribute("href", "/draft?project=p");
  expect(screen.getByRole("link", { name: /下载历史原稿|Download saved/ })).toHaveAttribute("href", "/api/v1/artifacts/old/content");
  client.clear();
});
