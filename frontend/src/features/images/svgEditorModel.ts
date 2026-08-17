export type BaseMode = "source" | "redrawn";
export type EditorTool = "select" | "marquee" | "erase" | "text" | "line" | "arrow";
export type ArrowStyle = "straight" | "orthogonal" | "arc" | "polyline";

export type Point = { x: number; y: number };
export type CropBox = { x: number; y: number; width: number; height: number };

type OperationBase = {
  id: string;
  color: string;
  width: number;
};

export type EraseOperation = OperationBase & {
  type: "erase";
  points: Point[];
  coordinateSpace?: "source";
};

export type LineOperation = OperationBase & {
  type: "line";
  start: Point;
  end: Point;
};

export type ArrowOperation = OperationBase & {
  type: "arrow";
  style: ArrowStyle;
  start: Point;
  end: Point;
  control?: Point;
  orthogonalRoute?: "horizontal-first" | "vertical-first";
  points?: Point[];
};

export type EditorOperation = EraseOperation | LineOperation | ArrowOperation;

type ElementBase = {
  id: string;
  x: number;
  y: number;
};

export type TextElement = ElementBase & {
  type: "text";
  text: string;
  color: string;
  fontSize: number;
};

export type KetcherElement = ElementBase & {
  type: "ketcher";
  ket: string;
  svgMarkup: string;
};

export type EditorElement = TextElement | KetcherElement;

export type TraceEdit = {
  id: string;
  dx: number;
  dy: number;
  hidden?: boolean;
};

export type SvgEditorState = {
  figureId: string;
  baseMode: BaseMode;
  vectorWidth: number;
  vectorHeight: number;
  sourceWidth: number;
  sourceHeight: number;
  traceMarkup: string;
  traceEdits: TraceEdit[];
  operations: EditorOperation[];
  elements: EditorElement[];
  crop: CropBox;
};

export type EditorSnapshot = Pick<SvgEditorState, "traceEdits" | "operations" | "elements" | "crop">;

export type SvgRenderOptions = {
  interactive?: boolean;
  selection?: string[];
  dragDelta?: Point | null;
  transientErase?: Point[];
  marquee?: CropBox | null;
};

const XML_ESCAPE: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&apos;",
};

function xml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => XML_ESCAPE[character]);
}

function number(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function positive(value: unknown, fallback: number): number {
  const parsed = number(value, fallback);
  return parsed > 0 ? parsed : fallback;
}

function point(value: unknown, fallback: Point): Point {
  const candidate = (value || {}) as Partial<Point>;
  return { x: number(candidate.x, fallback.x), y: number(candidate.y, fallback.y) };
}

function operationId(prefix: string, index: number): string {
  return `${prefix}-${Date.now()}-${index}`;
}

export function cloneSnapshot(state: SvgEditorState): EditorSnapshot {
  return structuredClone({
    traceEdits: state.traceEdits,
    operations: state.operations,
    elements: state.elements,
    crop: state.crop,
  });
}

export function restoreSnapshot(state: SvgEditorState, snapshot: EditorSnapshot): SvgEditorState {
  return {
    ...state,
    traceEdits: structuredClone(snapshot.traceEdits),
    operations: structuredClone(snapshot.operations),
    elements: structuredClone(snapshot.elements),
    crop: { ...snapshot.crop },
  };
}

function editableTraceMarkup(markup: string): string {
  const documentNode = new DOMParser().parseFromString(
    `<svg xmlns="http://www.w3.org/2000/svg"><g id="trace-root">${markup}</g></svg>`,
    "image/svg+xml",
  );
  const container = documentNode.querySelector("#trace-root");
  if (!container || documentNode.querySelector("parsererror")) return markup;
  const existing = [...container.querySelectorAll<SVGElement>("[data-trace-object-id]")];
  if (!existing.length) {
    const wrapper = documentNode.createElementNS("http://www.w3.org/2000/svg", "g");
    wrapper.setAttribute("data-trace-object-id", "trace-0");
    wrapper.setAttribute("data-vector-kind", "base-trace-object");
    while (container.firstChild) wrapper.appendChild(container.firstChild);
    container.appendChild(wrapper);
  }
  const used = new Set<string>();
  [...container.querySelectorAll<SVGElement>("[data-trace-object-id]")].forEach((node, index) => {
    let id = node.getAttribute("data-trace-object-id") || `trace-${index}`;
    while (used.has(id)) id = `${id}-${index}`;
    used.add(id);
    node.setAttribute("data-trace-object-id", id);
    node.setAttribute("data-select-key", `trace:${id}`);
  });
  return container.innerHTML;
}

export function normalizeAuditOperations(value: unknown): EditorOperation[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap<EditorOperation>((raw, index): EditorOperation[] => {
    if (!raw || typeof raw !== "object") return [];
    const candidate = raw as Record<string, unknown>;
    const type = String(candidate.type || "");
    const color = /^#[0-9a-f]{6}$/i.test(String(candidate.color || ""))
      ? String(candidate.color)
      : "#111111";
    const width = Math.max(1, number(candidate.width, type === "erase" ? 8 : 2));
    const id = String(candidate.id || operationId(type || "operation", index));
    if (type === "erase") {
      const points = Array.isArray(candidate.points)
        ? candidate.points.map((item) => point(item, { x: 0, y: 0 }))
        : [];
      return points.length > 1
        ? [{ id, type: "erase" as const, points, color: "#ffffff", width, coordinateSpace: "source" as const }]
        : [];
    }
    if (type === "line") {
      return [{
        id,
        type: "line" as const,
        start: point(candidate.start, { x: 0, y: 0 }),
        end: point(candidate.end, { x: 80, y: 0 }),
        color,
        width,
      }];
    }
    if (type === "arrow") {
      const legacyPoints = Array.isArray(candidate.points)
        ? candidate.points.map((item) => point(item, { x: 0, y: 0 }))
        : [];
      const start = legacyPoints[0] || point(candidate.start, { x: 0, y: 0 });
      const end = legacyPoints.at(-1) || point(candidate.end, { x: start.x + 80, y: start.y });
      const rawStyle = String(candidate.style || (legacyPoints.length > 2 ? "polyline" : "straight"));
      const style: ArrowStyle = ["straight", "orthogonal", "arc", "polyline"].includes(rawStyle)
        ? rawStyle as ArrowStyle
        : "straight";
      return [{
        id,
        type: "arrow" as const,
        style,
        start,
        end,
        control: candidate.control ? point(candidate.control, { x: (start.x + end.x) / 2, y: start.y - 48 }) : undefined,
        orthogonalRoute: candidate.orthogonalRoute === "vertical-first" ? "vertical-first" : "horizontal-first",
        points: legacyPoints.length > 2 ? legacyPoints : undefined,
        color,
        width,
      }];
    }
    return [];
  });
}

export function parseFullSvg(
  figureId: string,
  baseMode: BaseMode,
  markup: string,
  baseWidth?: number,
  baseHeight?: number,
): SvgEditorState {
  const documentNode = new DOMParser().parseFromString(markup, "image/svg+xml");
  const root = documentNode.documentElement;
  if (root.nodeName.toLowerCase() !== "svg" || documentNode.querySelector("parsererror")) {
    throw new Error("全图 SVG 格式无效。");
  }
  const viewBox = (root.getAttribute("viewBox") || "").trim().split(/\s+/).map(Number);
  const vectorWidth = positive(root.getAttribute("data-vector-width") || root.getAttribute("width") || viewBox[2], 1);
  const vectorHeight = positive(root.getAttribute("data-vector-height") || root.getAttribute("height") || viewBox[3], 1);
  const sourceWidth = positive(baseWidth || root.getAttribute("data-source-width") || root.getAttribute("data-original-width"), vectorWidth);
  const sourceHeight = positive(baseHeight || root.getAttribute("data-source-height") || root.getAttribute("data-original-height"), vectorHeight);
  const trace = root.querySelector("#full-image-vector-trace");
  if (!trace) throw new Error("全图 SVG 缺少完整矢量底图。");
  if (trace.querySelector("image")) throw new Error("全图 SVG 仍包含位图，已停止加载。");
  return {
    figureId,
    baseMode,
    vectorWidth,
    vectorHeight,
    sourceWidth,
    sourceHeight,
    traceMarkup: editableTraceMarkup(trace.innerHTML),
    traceEdits: [],
    operations: [],
    elements: [],
    crop: { x: 0, y: 0, width: vectorWidth, height: vectorHeight },
  };
}

function parseTranslate(value: string | null): Point {
  const matches = String(value || "").match(/translate\(\s*(-?[\d.]+)(?:[ ,]+(-?[\d.]+))?/i);
  return matches ? { x: number(matches[1]), y: number(matches[2]) } : { x: 0, y: 0 };
}

function decodeBase64Utf8(value: string): string {
  try {
    const bytes = Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  } catch {
    return "";
  }
}

export function mergeSavedSvg(
  state: SvgEditorState,
  markup: string,
  auditOperations: unknown,
): SvgEditorState {
  const documentNode = new DOMParser().parseFromString(markup, "image/svg+xml");
  const root = documentNode.documentElement;
  if (root.nodeName.toLowerCase() !== "svg" || documentNode.querySelector("parsererror")) {
    return { ...state, operations: normalizeAuditOperations(auditOperations) };
  }
  const cropEnabled = root.getAttribute("data-content-crop") === "true";
  const savedOutputWidth = positive(
    root.getAttribute("data-original-width") || root.getAttribute("width"),
    0,
  );
  const savedOutputHeight = positive(
    root.getAttribute("data-original-height") || root.getAttribute("height"),
    0,
  );
  // A cropped manual PNG becomes the next redrawn base. Legacy servers used to
  // vectorize that already-cropped PNG and then replay the old crop a second
  // time. When the saved SVG output is exactly the current base size, all edits
  // are already materialized in the trace and the old crop must not be applied.
  if (
    cropEnabled
    && Math.abs(savedOutputWidth - state.sourceWidth) < 0.5
    && Math.abs(savedOutputHeight - state.sourceHeight) < 0.5
  ) {
    return state;
  }
  const scaleX = state.vectorWidth / state.sourceWidth;
  const scaleY = state.vectorHeight / state.sourceHeight;
  const rawCrop = cropEnabled ? {
    x: number(root.getAttribute("data-crop-x")) * scaleX,
    y: number(root.getAttribute("data-crop-y")) * scaleY,
    width: positive(root.getAttribute("data-crop-width"), state.sourceWidth) * scaleX,
    height: positive(root.getAttribute("data-crop-height"), state.sourceHeight) * scaleY,
  } : state.crop;
  const cropX = Math.max(0, Math.min(state.vectorWidth - 1, rawCrop.x));
  const cropY = Math.max(0, Math.min(state.vectorHeight - 1, rawCrop.y));
  const crop = cropEnabled ? {
    x: cropX,
    y: cropY,
    width: Math.max(1, Math.min(rawCrop.width, state.vectorWidth - cropX)),
    height: Math.max(1, Math.min(rawCrop.height, state.vectorHeight - cropY)),
  } : state.crop;
  const elements: EditorElement[] = [];
  root.querySelectorAll<SVGElement>("[data-editor-element-id], [data-vector-kind='text'], [data-vector-kind='ketcher-structure']").forEach((node, index) => {
    const kind = node.getAttribute("data-editor-element-type") || node.getAttribute("data-vector-kind");
    const id = node.getAttribute("data-editor-element-id") || node.getAttribute("data-vector-index") || `saved-${index}`;
    const translated = parseTranslate(node.getAttribute("transform"));
    if (kind === "text") {
      const lines = [...node.querySelectorAll("tspan")].map((span) => span.textContent || "");
      elements.push({
        id,
        type: "text",
        x: number(node.getAttribute("data-editor-x") || node.getAttribute("x"), translated.x),
        y: number(node.getAttribute("data-editor-y") || node.getAttribute("y"), translated.y),
        text: lines.length ? lines.join("\n") : node.textContent || "",
        color: node.getAttribute("fill") || "#111111",
        fontSize: positive(node.getAttribute("font-size"), 16),
      });
    } else if (kind === "ketcher" || kind === "ketcher-structure") {
      const nested = node.querySelector("svg");
      if (!nested) return;
      elements.push({
        id,
        type: "ketcher",
        x: number(node.getAttribute("data-editor-x"), translated.x),
        y: number(node.getAttribute("data-editor-y"), translated.y),
        ket: decodeBase64Utf8(node.getAttribute("data-ketcher-ket") || ""),
        svgMarkup: new XMLSerializer().serializeToString(nested),
      });
    }
  });
  let traceEdits: TraceEdit[] = [];
  const traceMetadata = root.querySelector("#editor-trace-edits")?.textContent || "";
  if (traceMetadata) {
    try {
      const parsed = JSON.parse(traceMetadata);
      if (Array.isArray(parsed)) {
        traceEdits = parsed.flatMap<TraceEdit>((item): TraceEdit[] => {
          if (!item || typeof item !== "object") return [];
          const candidate = item as Partial<TraceEdit>;
          const id = String(candidate.id || "");
          if (!id) return [];
          return [{
            id,
            dx: number(candidate.dx),
            dy: number(candidate.dy),
            hidden: Boolean(candidate.hidden),
          }];
        });
      }
    } catch {
      traceEdits = [];
    }
  }
  return {
    ...state,
    crop,
    traceEdits,
    operations: normalizeAuditOperations(auditOperations),
    elements,
  };
}

export function clampPoint(state: SvgEditorState, value: Point): Point {
  return {
    x: Math.max(0, Math.min(state.vectorWidth, value.x)),
    y: Math.max(0, Math.min(state.vectorHeight, value.y)),
  };
}

export function moveOperation(operation: EditorOperation, dx: number, dy: number): EditorOperation {
  const move = (value: Point): Point => ({ x: value.x + dx, y: value.y + dy });
  if (operation.type === "erase") return operation;
  if (operation.type === "line") return { ...operation, start: move(operation.start), end: move(operation.end) };
  return {
    ...operation,
    start: move(operation.start),
    end: move(operation.end),
    control: operation.control ? move(operation.control) : undefined,
    points: operation.points?.map(move),
  };
}

function arrowHead(from: Point, end: Point, width: number): string {
  const angle = Math.atan2(end.y - from.y, end.x - from.x);
  const size = Math.max(8, width * 5);
  const left = { x: end.x - size * Math.cos(angle - Math.PI / 6), y: end.y - size * Math.sin(angle - Math.PI / 6) };
  const right = { x: end.x - size * Math.cos(angle + Math.PI / 6), y: end.y - size * Math.sin(angle + Math.PI / 6) };
  return `${end.x},${end.y} ${left.x},${left.y} ${right.x},${right.y}`;
}

export function arrowPath(operation: ArrowOperation): { d: string; from: Point } {
  if (operation.style === "polyline" && operation.points && operation.points.length > 1) {
    return {
      d: `M ${operation.points.map((item) => `${item.x} ${item.y}`).join(" L ")}`,
      from: operation.points.at(-2) || operation.start,
    };
  }
  if (operation.style === "orthogonal") {
    const middle = operation.orthogonalRoute === "vertical-first"
      ? { x: operation.start.x, y: operation.end.y }
      : { x: operation.end.x, y: operation.start.y };
    return {
      d: `M ${operation.start.x} ${operation.start.y} L ${middle.x} ${middle.y} L ${operation.end.x} ${operation.end.y}`,
      from: middle,
    };
  }
  if (operation.style === "arc") {
    const control = operation.control || {
      x: (operation.start.x + operation.end.x) / 2,
      y: Math.min(operation.start.y, operation.end.y) - 48,
    };
    return {
      d: `M ${operation.start.x} ${operation.start.y} Q ${control.x} ${control.y} ${operation.end.x} ${operation.end.y}`,
      from: control,
    };
  }
  return {
    d: `M ${operation.start.x} ${operation.start.y} L ${operation.end.x} ${operation.end.y}`,
    from: operation.start,
  };
}

function shiftedOperation(operation: EditorOperation, selected: Set<string>, delta?: Point | null): EditorOperation {
  return delta && selected.has(`op:${operation.id}`) ? moveOperation(operation, delta.x, delta.y) : operation;
}

function selectionFilter(key: string, selected: Set<string>): string {
  return selected.has(key) ? ' filter="url(#editor-selection-glow)"' : "";
}

function operationMarkup(operation: EditorOperation, interactive: boolean, selected: Set<string>): string {
  const key = `op:${operation.id}`;
  const data = interactive ? ` data-select-key="${xml(key)}"` : "";
  const filter = selectionFilter(key, selected);
  if (operation.type === "line") {
    const line = `<line${data}${filter} x1="${operation.start.x}" y1="${operation.start.y}" x2="${operation.end.x}" y2="${operation.end.y}" stroke="${xml(operation.color)}" stroke-width="${operation.width}" stroke-linecap="round"/>`;
    if (!interactive || !selected.has(key)) return line;
    return `${line}${handleMarkup(key, "start", operation.start)}${handleMarkup(key, "end", operation.end)}`;
  }
  if (operation.type !== "arrow") return "";
  const path = arrowPath(operation);
  const arrow = `<g${data}${filter}><path d="${path.d}" fill="none" stroke="${xml(operation.color)}" stroke-width="${operation.width}" stroke-linecap="round" stroke-linejoin="round"/><polygon points="${arrowHead(path.from, operation.end, operation.width)}" fill="${xml(operation.color)}"/></g>`;
  if (!interactive || !selected.has(key)) return arrow;
  const control = operation.style === "arc" && operation.control
    ? handleMarkup(key, "control", operation.control)
    : "";
  return `${arrow}${handleMarkup(key, "start", operation.start)}${handleMarkup(key, "end", operation.end)}${control}`;
}

function handleMarkup(key: string, kind: string, value: Point): string {
  return `<circle data-handle-key="${xml(key)}" data-handle-kind="${kind}" cx="${value.x}" cy="${value.y}" r="7" fill="#fff" stroke="#ef7d32" stroke-width="2"/>`;
}

function textMarkup(element: TextElement, interactive: boolean, selected: Set<string>, delta?: Point | null): string {
  const key = `el:${element.id}`;
  const dx = delta && selected.has(key) ? delta.x : 0;
  const dy = delta && selected.has(key) ? delta.y : 0;
  const x = element.x + dx;
  const y = element.y + dy;
  const data = interactive ? ` data-select-key="${xml(key)}"` : "";
  const spans = element.text.split(/\r?\n/).map((line, index) => (
    `<tspan x="${x}" dy="${index ? Math.round(element.fontSize * 1.25) : 0}">${xml(line)}</tspan>`
  )).join("");
  return `<text${data}${selectionFilter(key, selected)} data-editor-element-id="${xml(element.id)}" data-editor-element-type="text" data-vector-kind="text" data-editor-x="${x}" data-editor-y="${y}" x="${x}" y="${y}" fill="${xml(element.color)}" font-size="${element.fontSize}" font-family="Arial, Helvetica, sans-serif">${spans}</text>`;
}

function encodeBase64Utf8(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((item) => { binary += String.fromCharCode(item); });
  return btoa(binary);
}

function ketcherMarkup(element: KetcherElement, interactive: boolean, selected: Set<string>, delta?: Point | null): string {
  const key = `el:${element.id}`;
  const dx = delta && selected.has(key) ? delta.x : 0;
  const dy = delta && selected.has(key) ? delta.y : 0;
  const x = element.x + dx;
  const y = element.y + dy;
  const data = interactive ? ` data-select-key="${xml(key)}"` : "";
  return `<g${data}${selectionFilter(key, selected)} data-editor-element-id="${xml(element.id)}" data-editor-element-type="ketcher" data-vector-kind="ketcher-structure" data-editor-x="${x}" data-editor-y="${y}" data-ketcher-ket="${encodeBase64Utf8(element.ket)}" transform="translate(${x} ${y})">${element.svgMarkup}</g>`;
}

function sourcePixelCrop(state: SvgEditorState): { x: number; y: number; width: number; height: number } {
  const scaleX = state.sourceWidth / state.vectorWidth;
  const scaleY = state.sourceHeight / state.vectorHeight;
  const x = Math.max(0, Math.round(state.crop.x * scaleX));
  const y = Math.max(0, Math.round(state.crop.y * scaleY));
  return {
    x,
    y,
    width: Math.max(1, Math.min(state.sourceWidth - x, Math.round(state.crop.width * scaleX))),
    height: Math.max(1, Math.min(state.sourceHeight - y, Math.round(state.crop.height * scaleY))),
  };
}

export function outputPixelSize(state: SvgEditorState): { width: number; height: number } {
  const crop = sourcePixelCrop(state);
  return { width: crop.width, height: crop.height };
}

export function buildSvgDocument(state: SvgEditorState, options: SvgRenderOptions = {}): string {
  const interactive = Boolean(options.interactive);
  const selected = new Set(options.selection || []);
  const crop = state.crop;
  const pixelCrop = sourcePixelCrop(state);
  const cropped = crop.x > 0 || crop.y > 0 || crop.width !== state.vectorWidth || crop.height !== state.vectorHeight;
  const erasers = state.operations.filter((operation): operation is EraseOperation => operation.type === "erase");
  const mask = erasers.length ? `<mask id="editor-erase-mask" x="0" y="0" width="${state.vectorWidth}" height="${state.vectorHeight}" maskUnits="userSpaceOnUse" maskContentUnits="userSpaceOnUse"><rect x="0" y="0" width="${state.vectorWidth}" height="${state.vectorHeight}" fill="#fff"/>${erasers.map((operation) => `<polyline points="${operation.points.map((item) => `${item.x},${item.y}`).join(" ")}" fill="none" stroke="#000" stroke-width="${operation.width}" stroke-linecap="round" stroke-linejoin="round"/>`).join("")}</mask>` : "";
  const operations = state.operations.filter((operation) => operation.type !== "erase").map((operation) => (
    operationMarkup(shiftedOperation(operation, selected, options.dragDelta), interactive, selected)
  )).join("");
  const elements = state.elements.map((element) => (
    element.type === "text"
      ? textMarkup(element, interactive, selected, options.dragDelta)
      : ketcherMarkup(element, interactive, selected, options.dragDelta)
  )).join("");
  const transientErase = options.transientErase && options.transientErase.length > 1
    ? `<polyline points="${options.transientErase.map((item) => `${item.x},${item.y}`).join(" ")}" fill="none" stroke="#ef7d32" stroke-opacity=".7" stroke-width="2" stroke-dasharray="4 3"/>`
    : "";
  const marqueeBox = options.marquee;
  const marquee = interactive
    ? `<rect id="editor-marquee-overlay" x="${marqueeBox?.x || 0}" y="${marqueeBox?.y || 0}" width="${marqueeBox?.width || 0}" height="${marqueeBox?.height || 0}" fill="#1f6b5522" stroke="#1f6b55" stroke-width="1" stroke-dasharray="4 3" pointer-events="none"${marqueeBox ? "" : ' display="none"'}/>`
    : "";
  const selectionDefs = interactive ? '<filter id="editor-selection-glow" x="-30%" y="-30%" width="160%" height="160%"><feDropShadow dx="0" dy="0" stdDeviation="2" flood-color="#ef7d32" flood-opacity="1"/></filter>' : "";
  const traceEditMap = new Map(state.traceEdits.map((item) => [item.id, item]));
  const traceIds = new Set([
    ...state.traceEdits.map((item) => item.id),
    ...(options.selection || []).filter((key) => key.startsWith("trace:")).map((key) => key.slice(6)),
  ]);
  const traceRules = [...traceIds].map((id) => {
    const edit = traceEditMap.get(id);
    const selectedNow = selected.has(`trace:${id}`);
    const dx = (edit?.dx || 0) + (selectedNow ? options.dragDelta?.x || 0 : 0);
    const dy = (edit?.dy || 0) + (selectedNow ? options.dragDelta?.y || 0 : 0);
    const declarations = [
      dx || dy ? `transform:translate(${dx}px,${dy}px)` : "",
      edit?.hidden ? "display:none" : "",
      interactive && selectedNow ? "filter:url(#editor-selection-glow)" : "",
    ].filter(Boolean).join(";");
    return declarations
      ? `#full-image-vector-trace [data-trace-object-id="${xml(id)}"]{${declarations}}`
      : "";
  }).filter(Boolean).join("");
  const traceStyle = traceRules ? `<style id="editor-trace-style">${traceRules}</style>` : "";
  const persistedTraceEdits = state.traceEdits.filter((item) => item.hidden || item.dx || item.dy);
  const traceMetadata = `<metadata id="editor-trace-edits">${xml(JSON.stringify(persistedTraceEdits))}</metadata>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${crop.width}" height="${crop.height}" viewBox="0 0 ${crop.width} ${crop.height}" data-vector-width="${state.vectorWidth}" data-vector-height="${state.vectorHeight}" data-original-width="${pixelCrop.width}" data-original-height="${pixelCrop.height}" data-source-width="${state.sourceWidth}" data-source-height="${state.sourceHeight}" data-content-crop="${cropped ? "true" : "false"}" data-crop-unit="source-px" data-crop-x="${pixelCrop.x}" data-crop-y="${pixelCrop.y}" data-crop-width="${pixelCrop.width}" data-crop-height="${pixelCrop.height}"><title>Full-image chemistry figure vector trace with React SVG edits</title>${traceMetadata}<defs>${selectionDefs}${mask}${traceStyle}</defs><g transform="translate(${-crop.x} ${-crop.y})"><rect width="${state.vectorWidth}" height="${state.vectorHeight}" fill="#fff"/><g id="full-image-vector-trace"${erasers.length ? ' mask="url(#editor-erase-mask)"' : ""}>${state.traceMarkup}</g><g id="editor-inserted-elements">${elements}</g><g id="editable-arrow-overlays" data-base-mode="${state.baseMode}">${operations}</g>${transientErase}${marquee}</g></svg>`;
}

export function updateHandle(operation: EditorOperation, kind: string, value: Point): EditorOperation {
  if (operation.type === "erase") return operation;
  if (operation.type === "line") {
    return kind === "start" ? { ...operation, start: value } : { ...operation, end: value };
  }
  if (operation.style === "polyline" && operation.points?.length) {
    const points = [...operation.points];
    if (kind === "start") points[0] = value;
    if (kind === "end") points[points.length - 1] = value;
    return { ...operation, points, start: points[0], end: points[points.length - 1] };
  }
  if (kind === "start") return { ...operation, start: value };
  if (kind === "control") return { ...operation, control: value };
  return { ...operation, end: value };
}

export function operationForSave(operation: EditorOperation): Record<string, unknown> {
  if (operation.type === "erase") {
    return { type: "erase", id: operation.id, width: operation.width, points: operation.points, coordinateSpace: "source" };
  }
  if (operation.type === "line") {
    return { type: "line", id: operation.id, color: operation.color, width: operation.width, start: operation.start, end: operation.end };
  }
  return {
    type: "arrow",
    id: operation.id,
    style: operation.style,
    color: operation.color,
    width: operation.width,
    start: operation.start,
    end: operation.end,
    ...(operation.control ? { control: operation.control } : {}),
    ...(operation.points ? { points: operation.points } : {}),
    ...(operation.style === "orthogonal" ? { orthogonalRoute: operation.orthogonalRoute || "horizontal-first" } : {}),
  };
}
