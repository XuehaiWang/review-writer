import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SvgKetcherEditor,
  generatedSvg,
  loadFullSvgWorkspace,
  selectableKeysInClientBox,
  traceKeyNearClientPoint,
} from "./SvgKetcherEditor";

const SOURCE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50" data-original-width="200" data-original-height="100"><rect width="100" height="50" fill="#fff"/><g id="full-image-vector-trace"><path d="M2 2h8v1h-8z" fill="#111"/></g><g id="editable-arrow-overlays"/></svg>';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SvgKetcherEditor", () => {
  it("finds nearby source trace pixels without enlarging Ketcher hit areas", () => {
    const root = document.createElement("div");
    root.innerHTML = '<svg><g data-select-key="trace:letter"><path id="trace-pixel"/></g><g data-select-key="el:ketcher"><path id="ketcher-bond"/></g></svg>';
    document.body.appendChild(root);
    const tracePixel = root.querySelector("#trace-pixel")!;
    const ketcherBond = root.querySelector("#ketcher-bond")!;
    const originalElementsFromPoint = document.elementsFromPoint;
    Object.defineProperty(document, "elementsFromPoint", {
      configurable: true,
      value: (x: number) => x === 104 ? [tracePixel] : [ketcherBond],
    });
    try {
      expect(traceKeyNearClientPoint(root, 100, 50)).toBe("trace:letter");
    } finally {
      Object.defineProperty(document, "elementsFromPoint", {
        configurable: true,
        value: originalElementsFromPoint,
      });
      root.remove();
    }
  });

  it("includes thin source pixels touching a marquee edge and deduplicates objects", () => {
    const root = document.createElement("div");
    root.innerHTML = '<svg><g data-select-key="trace:text"><path/></g><g data-select-key="trace:outside"><path/></g></svg>';
    const textObject = root.querySelector<SVGElement>('[data-select-key="trace:text"]')!;
    const outsideObject = root.querySelector<SVGElement>('[data-select-key="trace:outside"]')!;
    textObject.getBoundingClientRect = () => ({ left: 19, right: 20, top: 10, bottom: 11, width: 1, height: 1, x: 19, y: 10, toJSON: () => ({}) });
    outsideObject.getBoundingClientRect = () => ({ left: 30, right: 31, top: 30, bottom: 31, width: 1, height: 1, x: 30, y: 30, toJSON: () => ({}) });
    expect(selectableKeysInClientBox(root, { left: 0, right: 18, top: 0, bottom: 18 })).toEqual(["trace:text"]);
  });

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

  it("updates selection glow and source-object deletion in the live SVG preview", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/v1/artifacts/live-preview-svg") {
        return new Response(SOURCE_SVG, { status: 200, headers: { "content-type": "image/svg+xml" } });
      }
      if (url.endsWith("/full-svg")) {
        return new Response(JSON.stringify({
          base_mode: "source",
          base_width: 200,
          base_height: 100,
          full_svg_url: "/api/v1/artifacts/live-preview-svg",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<SvgKetcherEditor
      projectId="project-live-preview"
      figureId="P002-F01"
      hasRedrawnBase={false}
      initialBaseMode="source"
      onClose={() => undefined}
      onSaved={() => undefined}
    />);
    await waitFor(() => expect(screen.getByText(/React SVG 工作区已就绪/)).toBeInTheDocument());
    const canvas = document.querySelector<HTMLElement>(".svg-react-canvas")!;
    const svg = canvas.querySelector<SVGSVGElement>("svg")!;
    svg.getBoundingClientRect = () => ({ left: 0, right: 100, top: 0, bottom: 50, width: 100, height: 50, x: 0, y: 0, toJSON: () => ({}) });
    const path = canvas.querySelector<SVGPathElement>("#full-image-vector-trace path")!;

    fireEvent.pointerDown(path, { clientX: 5, clientY: 3, pointerId: 1 });
    fireEvent.pointerUp(canvas, { clientX: 5, clientY: 3, pointerId: 1 });
    await waitFor(() => expect(screen.getByText("已选择 1 个对象，可拖动或删除。")).toBeInTheDocument());
    expect(canvas.querySelector('[data-select-key="trace:trace-0"]')).toHaveAttribute("data-editor-selected", "true");
    expect(canvas.querySelector('[data-select-key="trace:trace-0"]')).toHaveAttribute("filter", "url(#editor-selection-glow)");

    fireEvent.click(screen.getByRole("button", { name: /删除所选/ }));
    await waitFor(() => expect(screen.getByText("已删除 1 个对象。")).toBeInTheDocument());
    const hidden = canvas.querySelector<SVGElement>('[data-select-key="trace:trace-0"]')!;
    expect(hidden).toHaveAttribute("display", "none");
    expect(hidden.style.display).toBe("none");
  });

  it("reopens a saved Ketcher element as an independently resizable structure", async () => {
    const saved = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50" data-vector-width="100" data-vector-height="50" data-source-width="200" data-source-height="100" data-original-width="200" data-original-height="100" data-content-crop="false"><metadata id="editor-trace-edits">[]</metadata><g><rect width="100" height="50" fill="#fff"/><g id="full-image-vector-trace"><g data-trace-object-id="trace-0"><path d="M2 2h8v1h-8z"/></g></g><g id="editor-inserted-elements"><g data-editor-element-id="ket-saved" data-editor-element-type="ketcher" data-vector-kind="ketcher-structure" data-editor-x="20" data-editor-y="10" data-editor-width="20" data-editor-height="10" data-editor-scale="0.6" data-ketcher-ket="e30=" transform="translate(20 10) scale(0.6)"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" viewBox="0 0 20 10"><path d="M0 5L20 5"/></svg></g></g></g></svg>';
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/v1/artifacts/current-base") return new Response(SOURCE_SVG, { status: 200 });
      if (url === "/api/v1/artifacts/saved-workspace") return new Response(saved, { status: 200 });
      if (url === "/api/v1/artifacts/saved-audit") return new Response(JSON.stringify({ operations: [] }), { status: 200, headers: { "content-type": "application/json" } });
      if (url.endsWith("/full-svg")) return new Response(JSON.stringify({ base_mode: "source", base_width: 200, base_height: 100, full_svg_url: "/api/v1/artifacts/current-base" }), { status: 200, headers: { "content-type": "application/json" } });
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<SvgKetcherEditor
      projectId="project-saved-ketcher"
      figureId="P003-F01"
      row={{ editable_svg: "/api/v1/artifacts/saved-workspace", audit_url: "/api/v1/artifacts/saved-audit", manual_edit: { base_mode: "source" } }}
      hasRedrawnBase={false}
      initialBaseMode="source"
      onClose={() => undefined}
      onSaved={() => undefined}
    />);
    await waitFor(() => expect(document.querySelector('[data-editor-element-id="ket-saved"]')).not.toBeNull());
    expect(document.querySelector('[data-editor-element-id="ket-saved"]')).toHaveAttribute("data-editor-scale", "0.6");
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
