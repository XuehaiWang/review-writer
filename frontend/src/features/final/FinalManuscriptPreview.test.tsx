import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import { FinalManuscriptPreview } from "./FinalManuscriptPreview";
afterEach(cleanup);
it("previews Final read-only with navigation to Draft and historical downloads", () => {
  const client = new QueryClient();
  render(<MemoryRouter><QueryClientProvider client={client}><FinalManuscriptPreview projectId="p" markdown="Saved final" versions={[{ artifact_id: "old", current: false, operation: "final-build", created_at: "yesterday" }]} /></QueryClientProvider></MemoryRouter>);
  expect(screen.getByText("Saved final")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("link", { name: /前往初稿|Edit in Draft/ })).toHaveAttribute("href", "/draft?project=p");
  expect(screen.getByRole("link", { name: /下载历史原稿|Download saved/ })).toHaveAttribute("href", "/api/v1/artifacts/old/content");
  client.clear();
});
