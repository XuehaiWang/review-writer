import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { DraftPage } from "./DraftPage";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
vi.mock("../../components/ProjectSelector", () => ({ ProjectSelector: () => <span>Project selector</span>,
  useSelectedProject: () => ({ selected: { project_id: "p" } }) }));
const paragraph = { paragraph_key: "k", paragraph_id: "S1-p1", text: "Saved paragraph", text_sha256: "a".repeat(64) };
const draft = { revision: 1, draft_artifact_id: "draft1", paragraphs: [paragraph], first_draft_md: "Full manuscript text",
  sections: [{ section_id: "S1", title: "Chapter one", paragraphs: [paragraph] }], section_task_states: {},
  paragraph_task_states: {}, rewrite_candidates: [], quality: {}, freshness: { upstream_stale: false }, versions: [] };
beforeEach(() => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockImplementation(async path => {
    if (path === "/api/v1/me") return { user_id: "u" };
    if (path.endsWith("/draft")) return draft;
    if (path.includes("section-dialogues")) return { turns: [] };
    throw new Error("Unexpected request: " + path);
  });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear(); });
function RouteProbe() { const location = useLocation(); const navigate = useNavigate();
  return <><output data-testid="route">{location.search}</output><button onClick={() => navigate(-1)}>Browser back</button></>;
}
function mount(url = "/draft?project=p") {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <MemoryRouter initialEntries={[url]}><DraftPage /><RouteProbe /></MemoryRouter>
  </QueryClientProvider>);
}
it("renders the composed overview manuscript instead of raw editable prose", async () => {
  vi.mocked(apiRequest).mockImplementation(async path => {
    if (path === "/api/v1/me") return { user_id: "u" };
    if (path.endsWith("/draft")) return { ...draft, manuscript_preview_md: "# Title\n\n![Overview figure](/api/v1/artifacts/overview/content)\n\n*Saved overview caption.*\n\n## Introduction\n\nFull manuscript text" };
    if (path.endsWith("/overview")) return { revision: 1, overview_figure_exists: false };
    throw new Error("Unexpected request: " + path);
  });
  mount("/draft?project=p&tab=preview");
  expect(await screen.findByRole("img", { name: "Overview figure" })).toHaveAttribute("src", "/api/v1/artifacts/overview/content");
  expect(screen.getByText(/Saved overview caption\./)).toBeTruthy();
});
it("starts with the manuscript and opens optional tools without a numbered wizard", async () => {
  mount();
  const nav = await screen.findByRole("navigation", { name: "Draft workspace" });
  expect(screen.getByText("Full manuscript text")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Start batch revision" })).toBeNull();
  fireEvent.click(within(nav).getByRole("button", { name: "Batch revision" }));
  expect(screen.getByRole("button", { name: "Start batch revision" })).toBeEnabled();
  fireEvent.click(within(nav).getByRole("button", { name: "Manuscript" }));
  fireEvent.click(screen.getByRole("button", { name: "Discuss current chapter" }));
  expect(await screen.findByRole("button", { name: "View text" })).toBeTruthy();
  expect(screen.getByText("Full manuscript text")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Back to manuscript" }));
  expect(screen.queryByRole("button", { name: "View text" })).toBeNull();
  fireEvent.click(within(nav).getByRole("button", { name: "Approve for Final" }));
  expect(screen.getByRole("button", { name: "Approve and allow final stage" })).toBeEnabled();
  fireEvent.click(within(nav).getByText("More"));
  fireEvent.click(within(nav).getByRole("button", { name: "History" }));
  expect(screen.getByText("No historical versions yet.")).toBeTruthy();
});
it("restores the selected column from the URL and preserves paragraph deep links", async () => {
  const view = mount("/draft?project=p&tab=history");
  await screen.findByText("No historical versions yet.");
  view.unmount();
  mount("/draft?project=p&paragraph=S1-p1");
  fireEvent.click(await screen.findByRole("button", { name: "View text" }));
  expect(await screen.findByText("Saved paragraph")).toBeVisible();
  expect(screen.queryByLabelText("Revision scope")).not.toBeInTheDocument();
});

it("expands and restores dialogue without losing the input or remounting the reader", async () => {
  mount("/draft?project=p&paragraph=S1-p1");
  const expand = await screen.findByRole("button", { name: "Expand dialogue" });
  const input = screen.getByRole("textbox", { name: "Tell the AI how to improve this chapter" });
  fireEvent.change(input, { target: { value: "Compare the key results" } });
  const manuscript = screen.getByText("Full manuscript text");
  fireEvent.click(expand);
  expect(manuscript).not.toBeVisible();
  expect(input).toHaveValue("Compare the key results");
  fireEvent.click(screen.getByRole("button", { name: "Restore split view" }));
  expect(screen.getByText("Full manuscript text")).toBe(manuscript);
  expect(manuscript).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Tell the AI how to improve this chapter" })).toBe(input);
  expect(input).toHaveValue("Compare the key results");
});
it("late batch submission does not pull the user away from another column", async () => {
  const original = vi.mocked(apiRequest).getMockImplementation()!;
  let finish!: (value: unknown) => void;
  vi.mocked(apiRequest).mockImplementation((path, init) => path.endsWith("/dialogue-batch")
    ? new Promise(resolve => { finish = resolve; }) : original(path, init));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Batch revision" }));
  fireEvent.click(await screen.findByRole("button", { name: "Start batch revision" }));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "Manuscript" }));
  await act(async () => { finish({ id: "batch" }); });
  expect(screen.getByText("Full manuscript text")).toBeTruthy();
  expect(screen.getByTestId("route")).toHaveTextContent("tab=preview");
});
