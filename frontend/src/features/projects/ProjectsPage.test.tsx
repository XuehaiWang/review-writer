import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { ProjectsPage } from "./ProjectsPage";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));

const project = {
  project_id: "p1", slug: "cs", owner_user_id: "u1", topic: "Allenes",
  taxonomy_profile: "chemistry_general", model_tier: "deepseek-v4-flash",
  discovery_status: "approved", current_stage: "planning",
  completed_stages: ["discovery"],
};

beforeEach(() => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (path === "/api/v1/projects") return { items: [project], count: 1 };
    if (path === "/api/v1/taxonomy-profiles") {
      return { items: [
        { id: "general_academic", label_zh: "通用学术", label_en: "General Academic", description_zh: "", description_en: "", domain_rules_enabled: false },
        { id: "chemistry_general", label_zh: "通用化学", label_en: "General Chemistry", description_zh: "", description_en: "", domain_rules_enabled: true },
      ], default_profile: "chemistry_general" };
    }
    if (String(path).includes("/model-catalog")) return { items: [], default_tier: "deepseek-v4-flash" };
    if (String(path).endsWith("/taxonomy-profile") && options?.method === "PATCH") {
      throw new ApiError("confirm_downstream_invalidation is required", 409);
    }
    throw new Error(`unexpected ${path}`);
  });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear(); });

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><ProjectsPage /></MemoryRouter></QueryClientProvider>);
}

/** The create form has a profile select too; the card one owns the project. */
const cardProfileSelect = (container: HTMLElement) => {
  const select = container.querySelector<HTMLSelectElement>(".project-card .project-taxonomy-field select");
  if (!select) throw new Error("project card taxonomy select not found");
  return select;
};

const taxonomyPatches = () => vi.mocked(apiRequest).mock.calls
  .filter(([path, options]) => String(path).endsWith("/taxonomy-profile") && options?.method === "PATCH");

it("asks the backend before deciding a taxonomy change invalidates later stages", async () => {
  const { container } = mount();
  await screen.findByText("cs");
  fireEvent.change(cardProfileSelect(container), { target: { value: "general_academic" } });
  // The stage order is never re-derived in the browser: the first attempt always
  // sends confirm=false and lets the backend answer.
  await waitFor(() => expect(taxonomyPatches()).toHaveLength(1));
  expect(JSON.parse(String(taxonomyPatches()[0][1]?.body))).toEqual({
    taxonomy_profile: "general_academic", confirm_downstream_invalidation: false,
  });
  // A 409 is the backend asking for confirmation.
  expect(await screen.findByText("Confirm change")).toBeVisible();
});

it("does not request a change when the profile is unchanged", async () => {
  const { container } = mount();
  await screen.findByText("cs");
  fireEvent.change(cardProfileSelect(container), { target: { value: "chemistry_general" } });
  await new Promise(resolve => setTimeout(resolve, 50));
  expect(taxonomyPatches()).toHaveLength(0);
});
