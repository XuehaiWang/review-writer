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
it("starts with batch analysis and every workspace column changes its actual content", async () => {
  mount();
  const nav = await screen.findByRole("navigation", { name: "Draft workspace" });
  const buttons = within(nav).getAllByRole("button");
  expect(buttons.map(b => b.textContent)).toEqual(["01 Batch analysis & candidates", "02 Chapter dialogue", "03 Manuscript", "04 Approval", "History"]);
  expect(screen.getByRole("button", { name: "Start batch revision" })).toBeEnabled();
  fireEvent.click(buttons[1]);
  expect(await screen.findByText("Saved paragraph")).toBeTruthy();
  expect(screen.getByTestId("route")).toHaveTextContent("tab=dialogue");
  fireEvent.click(buttons[2]);
  expect(screen.getByText("Full manuscript text")).toBeTruthy();
  expect(screen.queryByText("Saved paragraph")).toBeNull();
  fireEvent.click(buttons[3]);
  expect(screen.getByRole("button", { name: "Approve and allow final stage" })).toBeEnabled();
  fireEvent.click(buttons[4]);
  expect(screen.getByText("No historical versions yet.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Browser back" }));
  expect(await screen.findByRole("button", { name: "Approve and allow final stage" })).toBeTruthy();
  fireEvent.click(buttons[0]);
  expect(screen.getByRole("button", { name: "Start batch revision" })).toBeTruthy();
});
it("restores the selected column from the URL and preserves paragraph deep links", async () => {
  const view = mount("/draft?project=p&tab=history");
  await screen.findByText("No historical versions yet.");
  view.unmount();
  mount("/draft?project=p&paragraph=S1-p1");
  await screen.findByText("Saved paragraph");
  fireEvent.click(screen.getByRole("button", { name: "More options" }));
  expect(screen.getByLabelText("Revision scope")).toHaveValue("k");
});
it("late batch submission does not pull the user away from another column", async () => {
  const original = vi.mocked(apiRequest).getMockImplementation()!;
  let finish!: (value: unknown) => void;
  vi.mocked(apiRequest).mockImplementation((path, init) => path.endsWith("/dialogue-batch")
    ? new Promise(resolve => { finish = resolve; }) : original(path, init));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Start batch revision" }));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "03 Manuscript" }));
  await act(async () => { finish({ id: "batch" }); });
  expect(screen.getByText("Full manuscript text")).toBeTruthy();
  expect(screen.getByTestId("route")).toHaveTextContent("tab=preview");
});
