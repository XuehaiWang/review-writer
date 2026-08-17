import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SvgKetcherEditor, generatedSvg, loadFullSvgWorkspace } from "./SvgKetcherEditor";

const SOURCE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50" data-original-width="200" data-original-height="100"><rect width="100" height="50" fill="#fff"/><g id="full-image-vector-trace"><path d="M2 2h8v1h-8z" fill="#111"/></g><g id="editable-arrow-overlays"/></svg>';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SvgKetcherEditor", () => {
  it("accepts a cross-frame Blob-like Ketcher export and fits it to the SVG canvas", async () => {
    const crossFrameBlob = {
      text: async () => '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 300"><path d="M0 10L600 10"/></svg>',
    };
    const result = await generatedSvg(crossFrameBlob, 240, 180);
    expect(result.width).toBe(240);
    expect(result.height).toBe(120);
    expect(result.markup).toContain('width="240"');
    expect(result.markup).toContain('data-ketcher-render="true"');
  });

  it("loads the full SVG inside React and opens Ketcher without legacy navigation", async () => {
    let fullSvgPosts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/v1/artifacts/full-svg") {
        return new Response(SOURCE_SVG, { status: 200, headers: { "content-type": "image/svg+xml" } });
      }
      if (url.endsWith("/full-svg")) {
        fullSvgPosts += 1;
        return new Response(JSON.stringify({
          base_mode: "source",
          base_width: 200,
          base_height: 100,
          full_svg_url: "/api/v1/artifacts/full-svg",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<StrictMode><SvgKetcherEditor
        projectId="project-1"
        figureId="P001-F01"
        hasRedrawnBase={false}
        initialBaseMode="source"
        onClose={() => undefined}
        onSaved={() => undefined}
      /></StrictMode>);

    await waitFor(() => expect(screen.getByText(/React SVG 工作区已就绪/)).toBeInTheDocument());
    expect(fullSvgPosts).toBe(1);
    expect(document.querySelector("#full-image-vector-trace path")).not.toBeNull();
    expect(screen.queryByRole("link", { name: /在线编辑/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Ketcher 添加结构" }));
    expect(screen.getByTitle("Ketcher chemical structure editor")).toHaveAttribute("src", "/assets/ketcher/standalone/index.html");
  });

  it("retries one transient first-open conflict", async () => {
    let attempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/full-svg")) {
        attempts += 1;
        if (attempts === 1) {
          return new Response(JSON.stringify({ error: { code: "WORKFLOW_CONFLICT", message: "conflict" } }), {
            status: 409,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({
          base_mode: "source",
          base_width: 200,
          base_height: 100,
          full_svg_url: "/api/v1/artifacts/retry-full-svg",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url === "/api/v1/artifacts/retry-full-svg") {
        return new Response(SOURCE_SVG, { status: 200, headers: { "content-type": "image/svg+xml" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const loaded = await loadFullSvgWorkspace("project-retry", "P009-F01", "source");
    expect(attempts).toBe(2);
    expect(loaded.markup).toContain("full-image-vector-trace");
  });
});
