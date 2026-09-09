import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { apiRequest, jsonBody } from "../../../api/client";
import { useUiText } from "../../../i18n/useUiText";
import {
  type ArrowOperation,
  type ArrowStyle,
  type BaseMode,
  type CropBox,
  type EditorSnapshot,
  type EditorTool,
  type KetcherElement,
  type Point,
  type SvgEditorState,
  type TextElement,
  buildSvgDocument,
  bindEraseOperation,
  clampPoint,
  cloneSnapshot,
  mergeSavedSvg,
  moveSelection,
  operationForSave,
  outputPixelSize,
  parseFullSvg,
  restoreSnapshot,
  updateHandle,
} from "./svgEditorModel";
import {
  TOOL_META,
  clampKetcherScale,
  errorMessage,
  generatedSvg,
  identifier,
  isInputTarget,
  ketcherScaleAtPoint,
  loadFullSvgWorkspace,
  localizeEditorStatus,
  materializeTextDraft,
  normalizeBox,
  pointDistance,
  selectableKey,
  selectableKeysInClientBox,
  svgToPng,
  traceKeyNearClientPoint,
  type TextDraft,
} from "./svgEditorSupport";

export {
  generatedSvg,
  loadFullSvgWorkspace,
  selectableKeysInClientBox,
  traceKeyNearClientPoint,
} from "./svgEditorSupport";

type EditorRow = {
  editable_svg?: string;
  audit_url?: string;
  manual_edit?: { base_mode?: BaseMode; audit_path?: string };
  manual_arrow_edit?: { base_mode?: BaseMode; audit_path?: string; editable_svg?: string };
};

type SvgKetcherEditorProps = {
  projectId: string;
  figureId: string;
  displayFigureId?: string;
  row?: EditorRow;
  hasRedrawnBase: boolean;
  initialBaseMode?: BaseMode;
  onClose: () => void;
  onSaved: () => Promise<unknown> | unknown;
};

type Status = { text: string; error?: boolean };

type PointerSession =
  | { kind: "erase"; points: Point[] }
  | { kind: "line"; id: string; start: Point }
  | { kind: "arrow"; id: string; start: Point }
  | { kind: "move"; start: Point; selection: string[]; delta: Point }
  | { kind: "handle"; id: string; handle: string }
  | { kind: "resize-ketcher"; id: string; anchor: Point; width: number; height: number; scale: number }
  | { kind: "marquee"; start: Point; clientStart: Point }
  | { kind: "text"; start: Point };

type KetcherApi = {
  getKet: () => Promise<string>;
  setMolecule: (value: string) => Promise<unknown>;
  generateImage: (value: string, options: { outputFormat: "svg" }) => Promise<Blob | string | { text: () => Promise<string> }>;
};

export function SvgKetcherEditor({
  projectId,
  figureId,
  displayFigureId = figureId,
  row,
  hasRedrawnBase,
  initialBaseMode = hasRedrawnBase ? "redrawn" : "source",
  onClose,
  onSaved,
}: SvgKetcherEditorProps) {
  const { language, text } = useUiText();
  const canvasRef = useRef<HTMLDivElement>(null);
  const textAreaRef = useRef<HTMLTextAreaElement>(null);
  const ketcherFrameRef = useRef<HTMLIFrameElement>(null);
  const pointerRef = useRef<PointerSession | null>(null);
  const pointerMoveFrameRef = useRef<number | null>(null);
  const pendingPointerMoveRef = useRef<Point | null>(null);
  const [baseMode, setBaseMode] = useState<BaseMode>(initialBaseMode);
  const [model, setModel] = useState<SvgEditorState | null>(null);
  const [history, setHistory] = useState<EditorSnapshot[]>([]);
  const [selection, setSelection] = useState<string[]>([]);
  const [tool, setTool] = useState<EditorTool>("select");
  const [arrowStyle, setArrowStyle] = useState<ArrowStyle>("straight");
  const [color, setColor] = useState("#111111");
  const [lineWidth, setLineWidth] = useState(2);
  const [eraseWidth, setEraseWidth] = useState(8);
  const [fontSize, setFontSize] = useState(16);
  const [cropPadding, setCropPadding] = useState(16);
  const [status, setStatus] = useState<Status>({ text: "正在准备 React SVG 工作区…" });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [cropping, setCropping] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [transientErase, setTransientErase] = useState<Point[]>([]);
  const [textDraft, setTextDraft] = useState<TextDraft | null>(null);
  const [ketcherOpen, setKetcherOpen] = useState(false);
  const [ketcherTarget, setKetcherTarget] = useState<string | null>(null);
  const [ketcherReady, setKetcherReady] = useState(false);
  const [ketcherBusy, setKetcherBusy] = useState(false);
  const [ketcherStatus, setKetcherStatus] = useState("正在加载本地 Ketcher…");

  const savedSvgUrl = row?.editable_svg || row?.manual_arrow_edit?.editable_svg || "";
  const auditUrl = row?.audit_url || row?.manual_edit?.audit_path || row?.manual_arrow_edit?.audit_path || "";
  const savedBaseMode = row?.manual_edit?.base_mode || row?.manual_arrow_edit?.base_mode;

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setStatus({ text: "正在把整张图转换为 React 可编辑 SVG…" });
      setSelection([]);
      setHistory([]);
      setDirty(false);
      try {
        const { response, markup } = await loadFullSvgWorkspace(
          projectId,
          figureId,
          baseMode,
        );
        let next = parseFullSvg(figureId, baseMode, markup, response.base_width, response.base_height);
        if (savedSvgUrl && auditUrl && savedBaseMode === baseMode) {
          try {
            const [saved, audit] = await Promise.all([
              apiRequest<string>(savedSvgUrl),
              apiRequest<{ operations?: unknown }>(auditUrl),
            ]);
            // Reopen the saved vector workspace itself instead of vectorizing
            // the materialized PNG and overlaying edits again. This preserves
            // Ketcher elements as independently movable/resizable structures
            // across save and refresh cycles.
            const savedWorkspace = parseFullSvg(figureId, baseMode, saved);
            next = mergeSavedSvg(savedWorkspace, saved, audit.operations);
          } catch {
            setStatus({ text: "已加载底图；旧编辑记录不可读，本次从干净画布继续。", error: true });
          }
        }
        if (cancelled) return;
        setModel(next);
        const saveSize = outputPixelSize(next);
        setStatus({
          text: `React SVG 工作区已就绪；底图：${baseMode === "redrawn" ? "AI 重绘图" : "原图"}；保存分辨率 ${saveSize.width}×${saveSize.height}。`,
        });
      } catch (error) {
        if (!cancelled) setStatus({ text: errorMessage(error), error: true });
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [auditUrl, baseMode, figureId, projectId, savedBaseMode, savedSvgUrl]);

  useEffect(() => {
    if (textDraft) requestAnimationFrame(() => textAreaRef.current?.focus());
  }, [textDraft?.existingId, textDraft?.x, textDraft?.y]);

  const pushHistory = useCallback((state: SvgEditorState) => {
    setHistory((items) => [...items.slice(-49), cloneSnapshot(state)]);
  }, []);

  const undo = useCallback(() => {
    if (!model || !history.length) {
      setStatus({ text: "没有可撤回的操作。" });
      return;
    }
    const snapshot = history.at(-1)!;
    setModel(restoreSnapshot(model, snapshot));
    setHistory((items) => items.slice(0, -1));
    setSelection([]);
    setDirty(true);
    setStatus({ text: "已撤回上一步。" });
  }, [history, model]);

  const deleteSelection = useCallback(() => {
    if (!model || !selection.length) {
      setStatus({ text: "请先选择一个或多个对象。", error: true });
      return;
    }
    pushHistory(model);
    const selected = new Set(selection);
    const selectedTraceIds = selection
      .filter((key) => key.startsWith("trace:"))
      .map((key) => key.slice(6));
    const traceEdits = new Map(model.traceEdits.map((item) => [item.id, item]));
    selectedTraceIds.forEach((id) => {
      const current = traceEdits.get(id);
      traceEdits.set(id, { id, dx: current?.dx || 0, dy: current?.dy || 0, hidden: true });
    });
    setModel({
      ...model,
      traceEdits: [...traceEdits.values()],
      operations: model.operations.filter((item) => !selected.has(`op:${item.id}`)),
      elements: model.elements.filter((item) => !selected.has(`el:${item.id}`)),
    });
    setSelection([]);
    setDirty(true);
    setStatus({ text: `已删除 ${selection.length} 个对象。` });
  }, [model, pushHistory, selection]);

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (isInputTarget(event.target)) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
        return;
      }
      if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        deleteSelection();
        return;
      }
      const shortcut = TOOL_META.find((item) => item.key.toLowerCase() === event.key.toLowerCase());
      if (shortcut) setTool(shortcut.value);
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [deleteSelection, undo]);

  // Trace/text/Ketcher selection is rendered imperatively below. Rebuilding a
  // large raster-traced SVG for every click makes browser paint updates lag or
  // disappear. Only operation selection changes the markup because arrows and
  // lines need their editable handles materialized.
  const structuralSelectionKey = selection.filter((key) => {
    if (key.startsWith("op:")) return true;
    if (!key.startsWith("el:")) return false;
    const id = key.slice(3);
    return model?.elements.some((item) => item.id === id && item.type === "ketcher");
  }).join("|");
  const displaySvg = useMemo(() => model ? buildSvgDocument(model, {
    interactive: true,
    selection: structuralSelectionKey ? structuralSelectionKey.split("|") : [],
  }) : "", [model, structuralSelectionKey]);

  useLayoutEffect(() => {
    const root = canvasRef.current;
    if (!root || !model) return;
    if (model.operations.some((op) => op.type === "erase" && !op.targets)) {
      try {
        // Old files lack object positions at erase time. Freeze their current
        // appearance once; never keep a canvas-wide hole alive during editing.
        setModel({ ...model, operations: model.operations.map((op) =>
          op.type === "erase" && !op.targets ? bindEraseOperation(model, op, root) : op) });
        setDirty(true);
      } catch (error) {
        setStatus({ text: errorMessage(error), error: true });
      }
      return;
    }
    const selected = new Set(selection);
    root.querySelectorAll<SVGElement>("[data-select-key]").forEach((node) => {
      // `translate` is used only for the in-flight drag preview. Once React
      // renders committed model coordinates it must not survive into the next
      // drag, otherwise the next preview starts from the original DOM offset.
      node.style.removeProperty("translate");
      const key = node.getAttribute("data-select-key") || "";
      if (selected.has(key)) {
        node.setAttribute("filter", "url(#editor-selection-glow)");
        node.setAttribute("data-editor-selected", "true");
      } else {
        node.removeAttribute("filter");
        node.removeAttribute("data-editor-selected");
      }
    });
    const traceEdits = new Map(model.traceEdits.map((item) => [item.id, item]));
    root.querySelectorAll<SVGElement>("[data-trace-object-id]").forEach((node) => {
      const edit = traceEdits.get(node.getAttribute("data-trace-object-id") || "");
      if (edit?.hidden) {
        node.setAttribute("display", "none");
        node.style.display = "none";
      } else {
        node.removeAttribute("display");
        node.style.removeProperty("display");
      }
      // Committed source-object positions now live on the SVG `transform`
      // attribute generated from the model. Do not mirror them into mutable
      // inline CSS: Chromium can drop that CSS when the object is clicked
      // again, which made it visibly jump back despite intact metadata.
      node.style.removeProperty("transform");
    });
  }, [displaySvg, model, selection]);

  useLayoutEffect(() => {
    const node = canvasRef.current?.querySelector<SVGPolylineElement>("#editor-transient-erase-overlay");
    if (!node) return;
    if (transientErase.length > 1) {
      node.setAttribute("points", transientErase.map((point) => `${point.x},${point.y}`).join(" "));
      node.removeAttribute("display");
    } else {
      node.setAttribute("display", "none");
    }
  }, [displaySvg, transientErase]);

  const canvasPoint = useCallback((event: React.PointerEvent): Point | null => {
    if (!model || !canvasRef.current) return null;
    const svg = canvasRef.current.querySelector("svg");
    if (!svg) return null;
    const bounds = svg.getBoundingClientRect();
    return clampPoint(model, {
      x: model.crop.x + (event.clientX - bounds.left) * model.crop.width / bounds.width,
      y: model.crop.y + (event.clientY - bounds.top) * model.crop.height / bounds.height,
    });
  }, [model]);

  const canvasPointAt = useCallback((clientX: number, clientY: number): Point | null => {
    if (!model || !canvasRef.current) return null;
    const svg = canvasRef.current.querySelector("svg");
    if (!svg) return null;
    const bounds = svg.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return null;
    return clampPoint(model, {
      x: model.crop.x + (clientX - bounds.left) * model.crop.width / bounds.width,
      y: model.crop.y + (clientY - bounds.top) * model.crop.height / bounds.height,
    });
  }, [model]);

  const showMarquee = useCallback((box: CropBox | null) => {
    const node = canvasRef.current?.querySelector<SVGRectElement>("#editor-marquee-overlay");
    if (!node) return;
    if (!box) {
      node.setAttribute("display", "none");
      return;
    }
    node.removeAttribute("display");
    node.setAttribute("x", String(box.x));
    node.setAttribute("y", String(box.y));
    node.setAttribute("width", String(box.width));
    node.setAttribute("height", String(box.height));
  }, []);

  const previewMove = useCallback((keys: string[], delta: Point) => {
    const root = canvasRef.current;
    if (!root) return;
    keys.forEach((key) => {
      root.querySelectorAll<SVGElement>(`[data-select-key="${key}"]`).forEach((node) => {
        const movable = node.parentElement?.hasAttribute("data-editor-erase-wrapper") ? node.parentElement : node;
        movable.style.translate = `${delta.x}px ${delta.y}px`;
      });
    });
  }, []);

  const clearMovePreview = useCallback((keys: string[]) => {
    const root = canvasRef.current;
    if (!root) return;
    keys.forEach((key) => {
      root.querySelectorAll<SVGElement>(`[data-select-key="${key}"]`).forEach((node) => {
        node.style.removeProperty("translate");
        if (node.parentElement?.hasAttribute("data-editor-erase-wrapper")) node.parentElement.style.removeProperty("translate");
      });
    });
  }, []);

  const previewKetcherScale = useCallback((id: string, scale: number, width: number, height: number, anchor: Point) => {
    const root = canvasRef.current;
    if (!root) return;
    const structure = [...root.querySelectorAll<SVGElement>("[data-editor-element-id]")]
      .find((node) => node.getAttribute("data-editor-element-id") === id);
    if (structure) {
      structure.setAttribute("transform", `translate(${anchor.x} ${anchor.y}) scale(${scale})`);
      structure.setAttribute("data-editor-scale", String(scale));
    }
    const overlay = [...root.querySelectorAll<SVGGElement>("[data-ketcher-resize-overlay]")]
      .find((node) => node.getAttribute("data-ketcher-resize-overlay") === id);
    if (!overlay) return;
    const scaledWidth = width * scale;
    const scaledHeight = height * scale;
    const box = overlay.querySelector("rect");
    const handle = overlay.querySelector("circle");
    box?.setAttribute("width", String(scaledWidth));
    box?.setAttribute("height", String(scaledHeight));
    handle?.setAttribute("cx", String(anchor.x + scaledWidth));
    handle?.setAttribute("cy", String(anchor.y + scaledHeight));
  }, []);

  const openTextEditor = useCallback((element: TextElement) => {
    setFontSize(element.fontSize);
    setColor(element.color);
    setTextDraft({
      x: element.x,
      y: element.y - element.fontSize,
      width: Math.max(140, element.text.length * element.fontSize * .6),
      height: Math.max(42, element.text.split(/\r?\n/).length * element.fontSize * 1.4),
      text: element.text,
      existingId: element.id,
    });
  }, []);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!model || loading || textDraft) return;
    const current = canvasPoint(event);
    if (!current) return;
    const target = event.target as Element;
    const handle = target.closest<SVGElement>("[data-handle-key]");
    const ketcherResizeHandle = target.closest<SVGElement>("[data-ketcher-resize-id]");
    const directKey = selectableKey(target);
    const key = directKey || (tool === "select"
      ? traceKeyNearClientPoint(event.currentTarget, event.clientX, event.clientY)
      : "");
    if (tool === "text" && key.startsWith("el:")) {
      const element = model.elements.find((item) => `el:${item.id}` === key);
      if (element?.type === "text") {
        event.preventDefault();
        openTextEditor(element);
        return;
      }
    }
    if (tool === "select" && ketcherResizeHandle) {
      const id = ketcherResizeHandle.getAttribute("data-ketcher-resize-id") || "";
      const element = model.elements.find((item): item is KetcherElement => item.id === id && item.type === "ketcher");
      if (!element) return;
      pushHistory(model);
      setSelection([`el:${id}`]);
      pointerRef.current = {
        kind: "resize-ketcher",
        id,
        anchor: { x: element.x, y: element.y },
        width: element.width,
        height: element.height,
        scale: element.scale,
      };
    } else if (tool === "select" && handle) {
      const handleKey = handle.getAttribute("data-handle-key") || "";
      const operationId = handleKey.replace(/^op:/, "");
      pushHistory(model);
      setSelection([handleKey]);
      pointerRef.current = { kind: "handle", id: operationId, handle: handle.getAttribute("data-handle-kind") || "end" };
    } else if (tool === "select" && key) {
      const nextSelection = event.shiftKey
        ? selection.includes(key) ? selection.filter((item) => item !== key) : [...selection, key]
        : selection.includes(key) ? selection : [key];
      setSelection(nextSelection);
      setStatus({ text: nextSelection.length ? `已选择 ${nextSelection.length} 个对象，可拖动或删除。` : "未选择对象。" });
      pushHistory(model);
      pointerRef.current = {
        kind: "move",
        start: current,
        selection: nextSelection,
        delta: { x: 0, y: 0 },
      };
    } else if (tool === "select") {
      setSelection([]);
      setStatus({ text: "未选择对象。" });
      return;
    } else if (tool === "marquee") {
      pointerRef.current = { kind: "marquee", start: current, clientStart: { x: event.clientX, y: event.clientY } };
      showMarquee({ x: current.x, y: current.y, width: 0, height: 0 });
    } else if (tool === "erase") {
      pushHistory(model);
      pointerRef.current = { kind: "erase", points: [current] };
      setTransientErase([current]);
    } else if (tool === "line") {
      pushHistory(model);
      const id = identifier("line");
      pointerRef.current = { kind: "line", id, start: current };
      setSelection([`op:${id}`]);
      setModel({ ...model, operations: [...model.operations, { id, type: "line", start: current, end: current, color, width: lineWidth }] });
    } else if (tool === "arrow") {
      pushHistory(model);
      const id = identifier("arrow");
      const operation: ArrowOperation = {
        id,
        type: "arrow",
        style: arrowStyle,
        start: current,
        end: current,
        color,
        width: lineWidth,
        orthogonalRoute: "horizontal-first",
        ...(arrowStyle === "arc" ? { control: { x: current.x, y: current.y - 48 } } : {}),
      };
      pointerRef.current = { kind: "arrow", id, start: current };
      setSelection([`op:${id}`]);
      setModel({ ...model, operations: [...model.operations, operation] });
    } else if (tool === "text") {
      pointerRef.current = { kind: "text", start: current };
      showMarquee({ x: current.x, y: current.y, width: 0, height: 0 });
    }
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* Pointer capture is optional. */ }
  };

  const applyPointerMoveAt = useCallback((clientX: number, clientY: number) => {
    const session = pointerRef.current;
    if (!session || !model) return;
    const current = canvasPointAt(clientX, clientY);
    if (!current) return;
    if (session.kind === "erase") {
      const previous = session.points.at(-1);
      if (!previous || pointDistance(previous, current) >= 0.75) session.points.push(current);
      setTransientErase([...session.points]);
    } else if (session.kind === "line") {
      setModel({ ...model, operations: model.operations.map((item) => item.id === session.id && item.type === "line" ? { ...item, end: current } : item) });
    } else if (session.kind === "arrow") {
      setModel({ ...model, operations: model.operations.map((item) => {
        if (item.id !== session.id || item.type !== "arrow") return item;
        const dx = current.x - session.start.x;
        const dy = current.y - session.start.y;
        return {
          ...item,
          end: current,
          orthogonalRoute: Math.abs(dy) > Math.abs(dx) ? "vertical-first" : "horizontal-first",
          control: item.style === "arc" ? { x: (session.start.x + current.x) / 2, y: Math.min(session.start.y, current.y) - Math.max(32, Math.abs(dx) * .18) } : item.control,
        };
      }) });
    } else if (session.kind === "move") {
      session.delta = {
        x: current.x - session.start.x,
        y: current.y - session.start.y,
      };
      previewMove(session.selection, session.delta);
    } else if (session.kind === "handle") {
      setModel({ ...model, operations: model.operations.map((item) => item.id === session.id ? updateHandle(item, session.handle, current) : item) });
    } else if (session.kind === "resize-ketcher") {
      session.scale = ketcherScaleAtPoint(session.anchor, session.width, session.height, current);
      previewKetcherScale(session.id, session.scale, session.width, session.height, session.anchor);
    } else if (session.kind === "marquee" || session.kind === "text") {
      showMarquee(normalizeBox(session.start, current));
    }
  }, [canvasPointAt, model, previewKetcherScale, previewMove, showMarquee]);

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    pendingPointerMoveRef.current = { x: event.clientX, y: event.clientY };
    if (pointerMoveFrameRef.current !== null) return;
    pointerMoveFrameRef.current = window.requestAnimationFrame(() => {
      pointerMoveFrameRef.current = null;
      const pending = pendingPointerMoveRef.current;
      pendingPointerMoveRef.current = null;
      if (pending) applyPointerMoveAt(pending.x, pending.y);
    });
  };

  const finishPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const session = pointerRef.current;
    if (!session || !model) return;
    if (pointerMoveFrameRef.current !== null) {
      window.cancelAnimationFrame(pointerMoveFrameRef.current);
      pointerMoveFrameRef.current = null;
    }
    pendingPointerMoveRef.current = null;
    applyPointerMoveAt(event.clientX, event.clientY);
    const current = canvasPoint(event) || ("start" in session ? session.start : { x: 0, y: 0 });
    if (session.kind === "erase") {
      if (session.points.length > 1) {
        try {
          if (!canvasRef.current) throw new Error("SVG canvas unavailable.");
          const operation = bindEraseOperation(model, { id: identifier("erase"), type: "erase", points: session.points, color: "#ffffff", width: eraseWidth, coordinateSpace: "source" }, canvasRef.current);
          setModel({ ...model, operations: [...model.operations, operation] });
          setDirty(true);
          setStatus({ text: "橡皮擦操作已加入；请确认没有擦到化学结构或文字。" });
        } catch (error) {
          setStatus({ text: errorMessage(error), error: true });
        }
      }
      setTransientErase([]);
    } else if (session.kind === "move") {
      const delta = session.delta;
      if (Math.hypot(delta.x, delta.y) > 0.5) {
        // Keep the transient preview in place until React has committed the
        // same coordinates to the SVG model. Clearing it before setModel makes
        // the source object visibly flash back to its old position for one
        // browser paint. The layout effect removes `translate` immediately
        // after the committed SVG is installed.
        setModel((currentModel) => currentModel
          ? moveSelection(currentModel, session.selection, delta)
          : currentModel);
        setDirty(true);
        setStatus({ text: "对象位置已更新。" });
      } else {
        clearMovePreview(session.selection);
      }
    } else if (session.kind === "marquee") {
      const left = Math.min(session.clientStart.x, event.clientX);
      const right = Math.max(session.clientStart.x, event.clientX);
      const top = Math.min(session.clientStart.y, event.clientY);
      const bottom = Math.max(session.clientStart.y, event.clientY);
      const keys = canvasRef.current
        ? selectableKeysInClientBox(canvasRef.current, { left, right, top, bottom })
        : [];
      setSelection(keys);
      setStatus({ text: keys.length ? `已框选 ${keys.length} 个对象，可直接删除。` : "框选区域内没有可编辑对象。" });
      showMarquee(null);
    } else if (session.kind === "text") {
      const box = normalizeBox(session.start, current);
      setTextDraft({
        x: box.x,
        y: box.y,
        width: Math.max(140, box.width),
        height: Math.max(48, box.height),
        text: "",
      });
      showMarquee(null);
    } else if (session.kind === "line" || session.kind === "arrow") {
      if (pointDistance(session.start, current) < 4) {
        const end = clampPoint(model, { x: session.start.x + Math.max(64, Math.min(120, model.vectorWidth * .08)), y: session.start.y });
        setModel({ ...model, operations: model.operations.map((item) => {
          if (item.id !== session.id || item.type === "erase") return item;
          return item.type === "line" ? { ...item, end } : { ...item, end };
        }) });
      }
      setDirty(true);
      setStatus({ text: session.kind === "line" ? "直线已添加。" : "箭头已添加；选择后可继续调整端点。" });
    } else if (session.kind === "handle") {
      setDirty(true);
      setStatus({ text: "端点位置已更新。" });
    } else if (session.kind === "resize-ketcher") {
      const scale = clampKetcherScale(session.scale);
      setModel({
        ...model,
        elements: model.elements.map((item) => item.id === session.id && item.type === "ketcher"
          ? { ...item, scale }
          : item),
      });
      setDirty(true);
      setStatus({ text: `结构尺寸已更新为 ${Math.round(scale * 100)}%。` });
    }
    pointerRef.current = null;
    try { event.currentTarget.releasePointerCapture(event.pointerId); } catch { /* Ignore unsupported capture. */ }
  };

  useEffect(() => () => {
    if (pointerMoveFrameRef.current !== null) window.cancelAnimationFrame(pointerMoveFrameRef.current);
  }, []);

  const commitText = useCallback(() => {
    if (!model || !textDraft) return;
    const result = materializeTextDraft(model, textDraft, color, fontSize);
    if (result.changed) pushHistory(model);
    setModel(result.state);
    setSelection(result.selection);
    setStatus({ text: result.message });
    setTextDraft(null);
    if (result.changed) setDirty(true);
  }, [color, fontSize, model, pushHistory, textDraft]);

  const applySelectedTextStyle = () => {
    if (!model) return;
    const id = selection.find((item) => item.startsWith("el:"))?.slice(3);
    const element = model.elements.find((item) => item.id === id);
    if (element?.type !== "text") {
      setStatus({ text: "请先选择一个文本对象。", error: true });
      return;
    }
    pushHistory(model);
    setModel({ ...model, elements: model.elements.map((item) => item.id === id && item.type === "text" ? { ...item, color, fontSize } : item) });
    setDirty(true);
    setStatus({ text: "文字颜色和字号已应用。" });
  };

  const selectedKetcher = model?.elements.find((item): item is KetcherElement => (
    item.type === "ketcher" && selection.includes(`el:${item.id}`)
  ));

  const updateSelectedKetcherScale = (value: number) => {
    if (!model || !selectedKetcher) {
      setStatus({ text: "请先选择一个 Ketcher 结构。", error: true });
      return;
    }
    const scale = clampKetcherScale(value);
    if (Math.abs(scale - selectedKetcher.scale) < .001) return;
    pushHistory(model);
    setModel({
      ...model,
      elements: model.elements.map((item) => item.id === selectedKetcher.id && item.type === "ketcher"
        ? { ...item, scale }
        : item),
    });
    setDirty(true);
    setStatus({ text: `结构尺寸已更新为 ${Math.round(scale * 100)}%。` });
  };

  const openKetcher = (target: string | null) => {
    setKetcherTarget(target);
    setKetcherOpen(true);
    setKetcherReady(false);
    setKetcherBusy(false);
    setKetcherStatus("正在加载本地 Ketcher…");
  };

  const onKetcherLoad = async () => {
    let attempts = 0;
    const waitForApi = async (): Promise<KetcherApi> => {
      const frameWindow = ketcherFrameRef.current?.contentWindow as (Window & { ketcher?: KetcherApi }) | null;
      if (frameWindow?.ketcher) return frameWindow.ketcher;
      if (attempts++ >= 80) throw new Error("Ketcher 初始化超时。");
      await new Promise((resolve) => window.setTimeout(resolve, 125));
      return waitForApi();
    };
    try {
      const api = await waitForApi();
      const target = model?.elements.find((item) => item.id === ketcherTarget && item.type === "ketcher") as KetcherElement | undefined;
      if (target?.ket) await api.setMolecule(target.ket);
      setKetcherReady(true);
      setKetcherStatus(target ? "已载入所选结构。" : "Ketcher 已就绪；绘制后插入当前 SVG 画布。" );
    } catch (error) {
      setKetcherStatus(errorMessage(error));
    }
  };

  const insertKetcher = async () => {
    if (!model || !ketcherReady) return;
    setKetcherBusy(true);
    setKetcherStatus("正在导出化学结构 SVG…");
    try {
      const api = (ketcherFrameRef.current?.contentWindow as (Window & { ketcher?: KetcherApi }) | null)?.ketcher;
      if (!api) throw new Error("Ketcher 尚未完成初始化。");
      const ket = await api.getKet();
      const target = model.elements.find((item): item is KetcherElement => item.id === ketcherTarget && item.type === "ketcher");
      const generated = await generatedSvg(
        await api.generateImage(ket, { outputFormat: "svg" }),
        target?.width || Math.max(24, model.crop.width * .2),
        target?.height || Math.max(24, model.crop.height * .2),
      );
      pushHistory(model);
      if (ketcherTarget) {
        setModel({ ...model, elements: model.elements.map((item) => item.id === ketcherTarget && item.type === "ketcher" ? { ...item, ket, svgMarkup: generated.markup, width: generated.width, height: generated.height } : item) });
        setSelection([`el:${ketcherTarget}`]);
        setStatus({ text: "Ketcher 化学结构已更新。" });
      } else {
        const id = identifier("ketcher");
        const element: KetcherElement = {
          id,
          type: "ketcher",
          x: model.crop.x + Math.max(0, (model.crop.width - generated.width) / 2),
          y: model.crop.y + Math.max(0, (model.crop.height - generated.height) / 2),
          ket,
          svgMarkup: generated.markup,
          width: generated.width,
          height: generated.height,
          scale: 1,
        };
        setModel({ ...model, elements: [...model.elements, element] });
        setSelection([`el:${id}`]);
        setStatus({ text: "Ketcher 化学结构已插入；可移动并拖动右下角调整大小。" });
      }
      setDirty(true);
      setKetcherOpen(false);
    } catch (error) {
      setKetcherStatus(errorMessage(error));
      setKetcherBusy(false);
    }
  };

  const cropCanvas = async () => {
    if (!model) return;
    setCropping(true);
    setSelection([]);
    setStatus({ text: "正在计算当前可见内容边界…" });
    try {
      const svg = buildSvgDocument(model);
      const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
      try {
        const image = await new Promise<HTMLImageElement>((resolve, reject) => {
          const node = new Image();
          node.onload = () => resolve(node);
          node.onerror = () => reject(new Error("无法读取当前 SVG 进行裁剪。"));
          node.src = url;
        });
        const width = Math.max(1, Math.round(model.crop.width));
        const height = Math.max(1, Math.round(model.crop.height));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) throw new Error("浏览器无法读取 SVG 像素。");
        context.drawImage(image, 0, 0, width, height);
        const pixels = context.getImageData(0, 0, width, height).data;
        let left = width; let top = height; let right = -1; let bottom = -1;
        for (let y = 0; y < height; y += 1) {
          for (let x = 0; x < width; x += 1) {
            const offset = (y * width + x) * 4;
            const visible = pixels[offset + 3] > 8 && (pixels[offset] < 252 || pixels[offset + 1] < 252 || pixels[offset + 2] < 252);
            if (!visible) continue;
            left = Math.min(left, x); top = Math.min(top, y); right = Math.max(right, x); bottom = Math.max(bottom, y);
          }
        }
        if (right < left || bottom < top) throw new Error("画布中没有可裁剪的可见内容。");
        const padding = Math.max(0, Math.min(100, cropPadding));
        left = Math.max(0, left - padding); top = Math.max(0, top - padding);
        right = Math.min(width, right + 1 + padding); bottom = Math.min(height, bottom + 1 + padding);
        if (left === 0 && top === 0 && right === width && bottom === height) {
          setStatus({ text: "当前内容已经贴合画布。" });
          return;
        }
        pushHistory(model);
        const crop = {
          x: model.crop.x + left,
          y: model.crop.y + top,
          width: right - left,
          height: bottom - top,
        };
        setModel({ ...model, crop });
        setDirty(true);
        const pixelSize = outputPixelSize({ ...model, crop });
        setStatus({ text: `画布已裁剪为 ${pixelSize.width}×${pixelSize.height}；可撤回或保存。` });
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      setStatus({ text: errorMessage(error), error: true });
    } finally {
      setCropping(false);
    }
  };

  const resetEditor = () => {
    if (!model || !window.confirm(text("确定清除当前所有手动编辑并恢复完整画布吗？", "Clear all current manual edits and restore the full canvas?"))) return;
    pushHistory(model);
    setModel({ ...model, traceEdits: [], operations: [], elements: [], crop: { x: 0, y: 0, width: model.vectorWidth, height: model.vectorHeight } });
    setSelection([]);
    setDirty(true);
    setStatus({ text: "已恢复到当前底图的初始状态。" });
  };

  const downloadSvg = () => {
    if (!model) return;
    const url = URL.createObjectURL(new Blob([buildSvgDocument(model)], { type: "image/svg+xml" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${displayFigureId}-online-edit.svg`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    setStatus({ text: "已下载当前全图 SVG。" });
  };

  const save = async () => {
    if (!model) return;
    let stateToSave = model;
    if (textDraft) {
      const result = materializeTextDraft(model, textDraft, color, fontSize);
      if (result.changed) pushHistory(model);
      stateToSave = result.state;
      setModel(result.state);
      setSelection(result.selection);
      setTextDraft(null);
    }
    setSaving(true);
    setStatus({ text: "正在保存 React SVG 编辑结果…" });
    try {
      const svg = buildSvgDocument(stateToSave);
      const size = outputPixelSize(stateToSave);
      const png = await svgToPng(svg, size.width, size.height);
      await apiRequest(
        `/api/v1/projects/${encodeURIComponent(projectId)}/figures/${encodeURIComponent(figureId)}/manual-edit`,
        {
          method: "POST",
          ...jsonBody({
            image_png_data_url: png,
            operations: stateToSave.operations.map(operationForSave),
            base_mode: stateToSave.baseMode,
            editable_svg: svg,
            full_vector_svg: svg,
          }),
        },
      );
      setDirty(false);
      setStatus({ text: "SVG 和 PNG 已保存，正在刷新第五阶段结果。" });
      await onSaved();
      onClose();
    } catch (error) {
      setStatus({ text: errorMessage(error), error: true });
    } finally {
      setSaving(false);
    }
  };

  const changeBaseMode = (next: BaseMode) => {
    if (next === baseMode) return;
    if (dirty && !window.confirm(text("切换底图会清除尚未保存的编辑，是否继续？", "Switching the base image clears unsaved edits. Continue?"))) return;
    setBaseMode(next);
  };

  const close = () => {
    if (dirty && !window.confirm(text("当前 SVG 修改尚未保存，确定关闭吗？", "Current SVG changes are unsaved. Close anyway?"))) return;
    onClose();
  };

  const currentTool = TOOL_META.find((item) => item.value === tool) || TOOL_META[0];
  const textPosition = model && textDraft ? {
    left: `${(textDraft.x - model.crop.x) / model.crop.width * 100}%`,
    top: `${(textDraft.y - model.crop.y) / model.crop.height * 100}%`,
    width: `${textDraft.width / model.crop.width * 100}%`,
    height: `${textDraft.height / model.crop.height * 100}%`,
  } : undefined;

  return <div className="svg-react-overlay" role="dialog" aria-modal="true" aria-label={`${displayFigureId} SVG editor`}>
    <section className="svg-react-workspace">
      <header className="svg-react-header">
        <div><span className="step-label">React SVG + Ketcher</span><h2>{displayFigureId} {text("在线编辑", "online editor")}</h2><p>{text("所有操作都在当前第五阶段页面完成，保存后立即刷新 Redrawn Output。", "All edits stay on the current Stage 5 page, and saving refreshes Redrawn Output immediately.")}</p></div>
        <div className="svg-react-header-actions">
          <label>{text("编辑底图", "Base image")}<select value={baseMode} onChange={(event) => changeBaseMode(event.target.value as BaseMode)} disabled={loading || saving}><option value="source">{text("原图", "Source")}</option><option value="redrawn" disabled={!hasRedrawnBase}>{text("AI 重绘图", "AI redraw")}</option></select></label>
          <button className="button button-quiet" type="button" onClick={close}>{text("关闭", "Close")}</button>
        </div>
      </header>
      <div className="svg-react-body">
        <aside className="svg-react-toolbar">
          <section><h3>{text("编辑工具", "Editing tools")}</h3><div className="svg-tool-grid-react">{TOOL_META.map((item) => <button key={item.value} className={tool === item.value ? "active" : ""} type="button" onClick={() => setTool(item.value)} title={`${text(item.hintZh, item.hintEn)} (${item.key})`}><span>{item.icon}</span><strong>{text(item.labelZh, item.labelEn)}</strong><small>{item.key}</small></button>)}</div><p className="svg-tool-hint-react">{text(currentTool.hintZh, currentTool.hintEn)}</p></section>
          <section className="svg-property-grid"><h3>{text("样式", "Style")}</h3>{tool === "arrow" ? <label>{text("箭头样式", "Arrow style")}<select value={arrowStyle} onChange={(event) => setArrowStyle(event.target.value as ArrowStyle)}><option value="straight">{text("直线箭头", "Straight arrow")}</option><option value="orthogonal">{text("自适应直角箭头", "Adaptive orthogonal arrow")}</option><option value="arc">{text("圆弧箭头", "Arc arrow")}</option></select></label> : null}<label>{text("颜色", "Color")}<input type="color" value={color} onChange={(event) => setColor(event.target.value)} /></label><label>{text("线宽", "Line width")}<input type="number" min="1" max="12" value={lineWidth} onChange={(event) => setLineWidth(Math.max(1, Number(event.target.value) || 2))} /></label>{tool === "erase" ? <label>{text("橡皮擦宽度", "Eraser width")}<input type="number" min="2" max="80" value={eraseWidth} onChange={(event) => setEraseWidth(Math.max(2, Number(event.target.value) || 8))} /></label> : null}{tool === "text" || selection.some((item) => item.startsWith("el:")) ? <><label>{text("字号", "Font size")}<input type="number" min="6" max="160" value={fontSize} onChange={(event) => setFontSize(Math.max(6, Number(event.target.value) || 16))} /></label><button className="button button-secondary" type="button" onClick={applySelectedTextStyle}>{text("应用文字样式", "Apply text style")}</button></> : null}</section>
          <section><h3>{text("化学结构", "Chemical structure")}</h3><button className="button button-secondary" type="button" onClick={() => openKetcher(null)}>{text("Ketcher 添加结构", "Add structure with Ketcher")}</button><button className="button button-secondary" type="button" disabled={!selectedKetcher} onClick={() => selectedKetcher && openKetcher(selectedKetcher.id)}>{text("编辑所选结构", "Edit selected structure")}</button>{selectedKetcher ? <div className="ketcher-size-controls"><label>{text("结构大小", "Structure size")}<input key={`${selectedKetcher.id}:${Math.round(selectedKetcher.scale * 100)}`} aria-label={text("Ketcher 结构大小百分比", "Ketcher structure size percentage")} type="number" min="10" max="500" step="5" defaultValue={Math.round(selectedKetcher.scale * 100)} onBlur={(event) => updateSelectedKetcherScale((Number(event.currentTarget.value) || 100) / 100)} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /></label><div><button className="button button-secondary" type="button" onClick={() => updateSelectedKetcherScale(selectedKetcher.scale - .1)}>−10%</button><button className="button button-secondary" type="button" onClick={() => updateSelectedKetcherScale(1)}>{text("重置", "Reset")}</button><button className="button button-secondary" type="button" onClick={() => updateSelectedKetcherScale(selectedKetcher.scale + .1)}>+10%</button></div><p>{text("也可拖动画布中结构右下角的圆形手柄等比例缩放。", "You can also drag the circular lower-right handle on the canvas for proportional resizing.")}</p></div> : null}</section>
          <section><h3>{text("画布和历史", "Canvas and history")}</h3><div className="svg-inline-field"><label>{text("裁剪留白", "Crop padding")}<input type="number" min="0" max="100" value={cropPadding} onChange={(event) => setCropPadding(Math.max(0, Math.min(100, Number(event.target.value) || 0)))} /></label><button className="button button-secondary" type="button" disabled={!model || cropping} onClick={() => void cropCanvas()}>{cropping ? text("计算中…", "Calculating…") : text("裁剪画布", "Crop canvas")}</button></div><button className="button button-secondary" type="button" disabled={!history.length} onClick={undo}>{text("撤回一步", "Undo")} Ctrl+Z</button><button className="button button-secondary" type="button" disabled={!selection.length} onClick={deleteSelection}>{text("删除所选", "Delete selected")} Delete</button><button className="button button-quiet" type="button" disabled={!model} onClick={resetEditor}>{text("恢复底图", "Restore base image")}</button></section>
        </aside>
        <main className="svg-react-main">
          <div className={status.error ? "svg-react-status error" : "svg-react-status"}>{localizeEditorStatus(status.text, language === "en")}</div>
          <div className="svg-react-canvas-scroll"><div ref={canvasRef} className={`svg-react-canvas tool-${tool}`} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={finishPointer} onPointerCancel={finishPointer}>{displaySvg ? <div className="svg-react-canvas-svg" dangerouslySetInnerHTML={{ __html: displaySvg }} /> : <div className="empty-state">{loading ? text("正在加载 SVG…", "Loading SVG…") : text("SVG 画布不可用。", "SVG canvas unavailable.")}</div>}{textDraft && textPosition ? <textarea ref={textAreaRef} className="svg-react-textbox" style={textPosition} value={textDraft.text} onChange={(event) => setTextDraft({ ...textDraft, text: event.target.value })} onBlur={commitText} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setTextDraft(null); } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); commitText(); } }} placeholder={text("输入文本；Ctrl+Enter 应用", "Enter text; Ctrl+Enter to apply")} /> : null}</div></div>
        </main>
      </div>
      <footer className="svg-react-footer"><div><strong>{dirty ? text("有未保存修改", "Unsaved changes") : text("当前修改已同步", "Current edits are synchronized")}</strong><span>{text("保存会同时生成 PNG、完整 SVG 和审核记录。", "Saving generates PNG, full SVG, and an audit record together.")}</span></div><button className="button button-secondary" type="button" disabled={!model} onClick={downloadSvg}>{text("下载 SVG", "Download SVG")}</button><button className="button button-primary" type="button" disabled={!model || loading || saving} onClick={() => void save()}>{saving ? text("保存中…", "Saving…") : text("保存 SVG 和 PNG", "Save SVG and PNG")}</button></footer>
    </section>
    {ketcherOpen ? <div className="ketcher-react-overlay" role="dialog" aria-modal="true" aria-label={text("Ketcher 化学结构编辑器", "Ketcher chemical structure editor")} onPointerDown={(event) => { if (event.target === event.currentTarget && !ketcherBusy) setKetcherOpen(false); }}><section className="ketcher-react-modal"><header><div><strong>{text("Ketcher 化学结构编辑", "Ketcher chemical structure editor")}</strong><p>{localizeEditorStatus(ketcherStatus, language === "en")}</p></div><button className="button button-quiet" type="button" disabled={ketcherBusy} onClick={() => setKetcherOpen(false)}>{text("关闭", "Close")}</button></header><iframe ref={ketcherFrameRef} title="Ketcher chemical structure editor" src="/assets/ketcher/standalone/index.html" onLoad={() => void onKetcherLoad()} /><footer><button className="button button-secondary" type="button" disabled={ketcherBusy} onClick={() => setKetcherOpen(false)}>{text("取消", "Cancel")}</button><button className="button button-primary" type="button" disabled={!ketcherReady || ketcherBusy} onClick={() => void insertKetcher()}>{ketcherBusy ? text("导出中…", "Exporting…") : ketcherTarget ? text("更新到 SVG 画布", "Update SVG canvas") : text("插入到 SVG 画布", "Insert into SVG canvas")}</button></footer></section></div> : null}
  </div>;
}
