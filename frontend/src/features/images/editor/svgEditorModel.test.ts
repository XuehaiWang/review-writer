import { describe, expect, it } from "vitest";

import {
  arrowPath,
  bindEraseOperation,
  operationForSave,
  buildSvgDocument,
  mergeSavedSvg,
  moveOperation,
  moveSelection,
  normalizeAuditOperations,
  outputPixelSize,
  parseFullSvg,
} from "./svgEditorModel";

const SOURCE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50" data-original-width="200" data-original-height="100"><rect width="100" height="50" fill="#fff"/><g id="full-image-vector-trace"><path d="M2 2h8v1h-8z" fill="#111"/></g><g id="editable-arrow-overlays"/></svg>';

describe("React SVG editor model", () => {
  it("loads a server trace and preserves source dimensions", () => {
    const state = parseFullSvg("P001-F01", "source", SOURCE_SVG, 200, 100);
    expect(state.vectorWidth).toBe(100);
    expect(state.vectorHeight).toBe(50);
    expect(state.sourceWidth).toBe(200);
    expect(state.traceMarkup).toContain("<path");
    expect(state.traceMarkup).toContain('data-select-key="trace:trace-0"');
    expect(outputPixelSize(state)).toEqual({ width: 200, height: 100 });
  });

  it("persists moving and hiding selectable source trace objects", () => {
    const initial = parseFullSvg("P001-F01", "source", SOURCE_SVG, 200, 100);
    const edited = {
      ...initial,
      traceEdits: [{ id: "trace-0", dx: 6, dy: -2, hidden: true }],
    };
    const interactive = buildSvgDocument(edited, {
      interactive: true,
      selection: ["trace:trace-0"],
      dragDelta: { x: 2, y: 3 },
    });
    expect(interactive).toContain('data-select-key="trace:trace-0"');
    expect(interactive).toContain('transform="translate(8 1)"');
    expect(interactive).toContain('display="none"');
    const saved = buildSvgDocument(edited);
    const savedBase = parseFullSvg("P001-F01", "source", saved, 200, 100);
    expect(savedBase.traceMarkup).not.toContain("data-editor-trace-materialized");
    expect(savedBase.traceMarkup).not.toContain("translate(6 -2)");
    const restored = mergeSavedSvg(savedBase, saved, []);
    expect(restored.traceEdits).toEqual([{ id: "trace-0", dx: 6, dy: -2, hidden: true }]);
    expect(buildSvgDocument(restored)).toContain('transform="translate(6 -2)"');
  });

  it("serializes masks, editable objects, crop metadata, and a complete trace", () => {
    const initial = parseFullSvg("P001-F01", "redrawn", SOURCE_SVG, 200, 100);
    const state = {
      ...initial,
      crop: { x: 10, y: 5, width: 80, height: 40 },
      operations: normalizeAuditOperations([
        { id: "erase-1", type: "erase", width: 8, points: [{ x: 3, y: 4 }, { x: 8, y: 9 }], targets: [{ id: "trace-0", dx: 0, dy: 0, matrix: [1, 0, 0, 1, 0, 0], bounds: { x: -100, y: -50, width: 300, height: 150 } }] },
        { id: "line-1", type: "line", color: "#111111", width: 2, start: { x: 10, y: 10 }, end: { x: 30, y: 10 } },
        { id: "arrow-1", type: "arrow", style: "orthogonal", color: "#123456", width: 2, start: { x: 20, y: 20 }, end: { x: 40, y: 35 }, orthogonalRoute: "vertical-first" },
      ]),
      elements: [
        { id: "text-1", type: "text" as const, x: 12, y: 18, text: "R¹\nR²", color: "#111111", fontSize: 12 },
        { id: "ket-1", type: "ketcher" as const, x: 50, y: 20, ket: "{}", svgMarkup: '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><path d="M0 5L20 5"/></svg>', width: 20, height: 10, scale: .75 },
      ],
    };
    const markup = buildSvgDocument(state);
    const documentNode = new DOMParser().parseFromString(markup, "image/svg+xml");
    expect(documentNode.querySelector("parsererror")).toBeNull();
    expect(documentNode.querySelector("#full-image-vector-trace path")).not.toBeNull();
    expect(documentNode.querySelector("#editor-object-erase-0")).not.toBeNull();
    expect(documentNode.querySelector("[data-editor-element-type='text']")?.textContent).toContain("R¹");
    expect(documentNode.querySelector("[data-editor-element-type='ketcher'] svg")).not.toBeNull();
    expect(documentNode.querySelector("[data-editor-element-type='ketcher']")?.getAttribute("data-editor-scale")).toBe("0.75");
    expect(documentNode.documentElement.getAttribute("data-content-crop")).toBe("true");
    expect(documentNode.documentElement.getAttribute("data-crop-x")).toBe("20");
    expect(documentNode.documentElement.getAttribute("data-crop-width")).toBe("160");
    expect(outputPixelSize(state)).toEqual({ width: 160, height: 80 });

    const restored = mergeSavedSvg(initial, markup, []);
    expect(restored.elements.find((item) => item.type === "ketcher")).toMatchObject({
      width: 20,
      height: 10,
      scale: .75,
    });

    const interactive = buildSvgDocument(state, { interactive: true, selection: ["el:ket-1"] });
    const interactiveDocument = new DOMParser().parseFromString(interactive, "image/svg+xml");
    expect(interactiveDocument.querySelector('[data-ketcher-resize-id="ket-1"]')).not.toBeNull();
    expect(interactiveDocument.querySelector('[data-ketcher-resize-overlay="ket-1"] rect')?.getAttribute("width")).toBe("15");
  });

  it("restores saved React elements and audit operations", () => {
    const initial = parseFullSvg("P001-F01", "source", SOURCE_SVG, 200, 100);
    const edited = {
      ...initial,
      operations: normalizeAuditOperations([{ type: "line", start: { x: 1, y: 2 }, end: { x: 9, y: 2 } }]),
      elements: [{ id: "text-1", type: "text" as const, x: 10, y: 20, text: "Co(II)", color: "#222222", fontSize: 14 }],
    };
    const saved = buildSvgDocument(edited);
    const restored = mergeSavedSvg(initial, saved, [{ type: "line", start: { x: 1, y: 2 }, end: { x: 9, y: 2 } }]);
    expect(restored.elements).toHaveLength(1);
    expect(restored.elements[0]).toMatchObject({ type: "text", text: "Co(II)", x: 10, y: 20 });
    expect(restored.operations).toHaveLength(1);
    expect(restored.operations[0].type).toBe("line");
  });

  it("does not apply a saved crop twice when its output is already the current base", () => {
    const currentBase = '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="570" viewBox="0 0 1024 570" data-original-width="1024" data-original-height="570" data-source-width="1024" data-source-height="570"><g id="full-image-vector-trace"><path d="M0 0h1024v570H0z"/></g></svg>';
    const state = parseFullSvg("P001-F01", "redrawn", currentBase, 1024, 570);
    const oldSavedWorkspace = '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="570" viewBox="0 0 1024 570" data-vector-width="1024" data-vector-height="784" data-original-width="1024" data-original-height="570" data-source-width="1024" data-source-height="784" data-content-crop="true" data-crop-unit="source-px" data-crop-x="0" data-crop-y="214" data-crop-width="1024" data-crop-height="570"><g transform="translate(0 -214)"><g id="full-image-vector-trace"><path d="M0 214h1024v570H0z"/></g></g></svg>';

    const restored = mergeSavedSvg(state, oldSavedWorkspace, []);

    expect(restored.crop).toEqual({ x: 0, y: 0, width: 1024, height: 570 });
    expect(buildSvgDocument(restored)).toContain('data-content-crop="false"');
  });

  it("keeps orthogonal routing and moves an entire arrow", () => {
    const arrow = normalizeAuditOperations([{ type: "arrow", style: "orthogonal", orthogonalRoute: "vertical-first", start: { x: 1, y: 2 }, end: { x: 10, y: 20 } }])[0];
    expect(arrow.type).toBe("arrow");
    if (arrow.type !== "arrow") throw new Error("Expected arrow");
    expect(arrowPath(arrow).d).toBe("M 1 2 L 1 20 L 10 20");
    expect(moveOperation(arrow, 5, -2)).toMatchObject({ start: { x: 6, y: 0 }, end: { x: 15, y: 18 } });
  });

  it("accumulates consecutive moves from the last committed position", () => {
    const initial = parseFullSvg("P001-F01", "source", SOURCE_SVG, 200, 100);
    const first = moveSelection(initial, ["trace:trace-0"], { x: 10, y: 5 });
    const second = moveSelection(first, ["trace:trace-0"], { x: 5, y: 2 });

    expect(second.traceEdits).toEqual([{
      id: "trace-0",
      dx: 15,
      dy: 7,
      hidden: undefined,
    }]);
    expect(buildSvgDocument(second)).toContain('transform="translate(15 7)"');
  });
});


it("keeps erasure attached to each object through move, crop and SVG-only reopen", () => {
  const markup = SOURCE_SVG.replace('<path d="M2 2h8v1h-8z" fill="#111"/>',
    '<g data-trace-object-id="a"><path d="M2 2h8v1h-8z"/></g><g data-trace-object-id="b"><path d="M50 2h8v1h-8z"/></g>');
  const initial = parseFullSvg("P1", "source", markup, 200, 100);
  const targets = ["a", "b"].map((id) => ({ id, dx: 0, dy: 0, matrix: [1, 0, 0, 1, 0, 0], bounds: { x: -100, y: -50, width: 300, height: 150 } }));
  const erased = { ...initial, operations: normalizeAuditOperations([{ type: "erase", id: "e1", width: 8, points: [{ x: 2, y: 2 }, { x: 10, y: 2 }], targets }]) };
  const moved = moveSelection(erased, ["trace:b"], { x: -48, y: 0 });
  const exported = buildSvgDocument({ ...moved, crop: { x: 1, y: 1, width: 90, height: 40 } });
  const doc = new DOMParser().parseFromString(exported, "image/svg+xml");
  expect(doc.querySelector("#full-image-vector-trace")?.hasAttribute("mask")).toBe(false);
  expect(doc.querySelector("#editor-object-erase-0 polyline")?.getAttribute("transform")).toBe("translate(0 0) matrix(1 0 0 1 0 0)");
  expect(doc.querySelector("#editor-object-erase-1 polyline")?.getAttribute("transform")).toBe("translate(-48 0) matrix(1 0 0 1 0 0)");
  const restored = mergeSavedSvg(parseFullSvg("P1", "source", exported, 200, 100), exported, []);
  expect(restored.operations.map(operationForSave)).toEqual(moved.operations.map(operationForSave));
  const reopened = new DOMParser().parseFromString(buildSvgDocument(restored), "image/svg+xml");
  expect(reopened.querySelectorAll("[data-editor-erase-wrapper]")).toHaveLength(2);
  expect(reopened.querySelectorAll("mask")).toHaveLength(2);
  expect(reopened.querySelector("#editor-object-erase-1 polyline")?.getAttribute("transform")).toBe("translate(-48 0) matrix(1 0 0 1 0 0)");
});

it("captures transformed object coordinates and excludes deleted objects", () => {
  const initial = parseFullSvg("P1", "source", SOURCE_SVG, 200, 100);
  const canvas = document.createElement("div");
  canvas.innerHTML = buildSvgDocument(initial);
  const trace = canvas.querySelector("#full-image-vector-trace")!;
  const matrix = { a: .5, b: 0, c: 0, d: .25, e: 7, f: -3 };
  Object.assign(trace, { getScreenCTM: () => ({ inverse: () => ({ multiply: () => matrix }) }) });
  Object.assign(canvas.querySelector("[data-trace-object-id]")!, {
    getScreenCTM: () => ({}), getBBox: () => ({ x: 0, y: 0, width: 10, height: 10 }),
  });
  const op = normalizeAuditOperations([{ type: "erase", points: [{ x: 1, y: 2 }, { x: 8, y: 2 }], width: 8 }])[0];
  if (op.type !== "erase") throw new Error("Missing erase fixture");
  const bound = bindEraseOperation({ ...initial, traceEdits: [{ id: "trace-0", dx: 12, dy: 3 }] }, op, canvas);
  expect(bound.targets?.[0]).toMatchObject({ id: "trace-0", dx: 12, dy: 3, matrix: [.5, 0, 0, .25, 7, -3] });
  expect(bindEraseOperation({ ...initial, traceEdits: [{ id: "trace-0", dx: 0, dy: 0, hidden: true }] }, op, canvas).targets).toEqual([]);
});
