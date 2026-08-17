import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, apiRequest, jsonBody } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";
import {
  type ArrowOperation,
  type ArrowStyle,
  type BaseMode,
  type CropBox,
  type EditorElement,
  type EditorSnapshot,
  type EditorTool,
  type KetcherElement,
  type Point,
  type SvgEditorState,
  type TextElement,
  buildSvgDocument,
  clampPoint,
  cloneSnapshot,
  mergeSavedSvg,
  moveOperation,
  operationForSave,
  outputPixelSize,
  parseFullSvg,
  restoreSnapshot,
  updateHandle,
} from "./svgEditorModel";

type FullSvgResponse = {
  base_mode: BaseMode;
  base_width: number;
  base_height: number;
  full_svg_url?: string;
  full_svg?: string;
};

type FullSvgWorkspace = { response: FullSvgResponse; markup: string };

const fullSvgWorkspaceLoads = new Map<string, Promise<FullSvgWorkspace>>();

export function loadFullSvgWorkspace(
  projectId: string,
  figureId: string,
  baseMode: BaseMode,
): Promise<FullSvgWorkspace> {
  const key = `${projectId}:${figureId}:${baseMode}`;
  const existing = fullSvgWorkspaceLoads.get(key);
  if (existing) return existing;
  const request = (async () => {
    let response: FullSvgResponse | undefined;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        response = await apiRequest<FullSvgResponse>(
          `/api/v1/projects/${encodeURIComponent(projectId)}/figures/${encodeURIComponent(figureId)}/full-svg`,
          { method: "POST", ...jsonBody({ base_mode: baseMode }) },
        );
        break;
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 409 || attempt > 0) throw error;
        await new Promise((resolve) => globalThis.setTimeout(resolve, 120));
      }
    }
    if (!response) throw new Error("服务器没有返回全图 SVG。" );
    const fullSvgUrl = response.full_svg_url || response.full_svg;
    if (!fullSvgUrl) throw new Error("服务器没有返回全图 SVG。" );
    return { response, markup: await apiRequest<string>(fullSvgUrl) };
  })();
  fullSvgWorkspaceLoads.set(key, request);
  const release = () => globalThis.setTimeout(() => {
    if (fullSvgWorkspaceLoads.get(key) === request) fullSvgWorkspaceLoads.delete(key);
  }, 750);
  void request.then(release, release);
  return request;
}

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
  | { kind: "move"; start: Point; selection: string[] }
  | { kind: "handle"; id: string; handle: string }
  | { kind: "marquee"; start: Point; clientStart: Point }
  | { kind: "text"; start: Point };

type TextDraft = {
  x: number;
  y: number;
  width: number;
  height: number;
  text: string;
  existingId?: string;
};

type TextMaterialization = {
  state: SvgEditorState;
  selection: string[];
  message: string;
  changed: boolean;
};

type KetcherApi = {
  getKet: () => Promise<string>;
  setMolecule: (value: string) => Promise<unknown>;
  generateImage: (value: string, options: { outputFormat: "svg" }) => Promise<Blob | string | { text: () => Promise<string> }>;
};

type GeneratedKetcherSvg = { markup: string; width: number; height: number };

const TOOL_META: Array<{ value: EditorTool; icon: string; labelZh: string; labelEn: string; key: string; hintZh: string; hintEn: string }> = [
  { value: "select", icon: "↖", labelZh: "选择 / 移动", labelEn: "Select / move", key: "V", hintZh: "可点选原图线条、文字、结构和新增对象并拖动。", hintEn: "Select and move source lines, text, structures, and inserted objects." },
  { value: "marquee", icon: "▧", labelZh: "框选对象", labelEn: "Marquee select", key: "M", hintZh: "拖出矩形，可批量选择原图矢量对象和新增对象。", hintEn: "Drag a rectangle to select source vector objects and inserted objects in a batch." },
  { value: "erase", icon: "⌫", labelZh: "橡皮擦", labelEn: "Eraser", key: "E", hintZh: "只擦除原图矢量层，不覆盖后来插入的内容。", hintEn: "Erase only the base vector layer without covering later insertions." },
  { value: "text", icon: "T", labelZh: "文本框", labelEn: "Text box", key: "T", hintZh: "拖出文本框；点已有文字可再次编辑。", hintEn: "Drag out a text box; select existing text to edit it again." },
  { value: "line", icon: "╱", labelZh: "直线", labelEn: "Line", key: "L", hintZh: "按下并拖动确定直线的起点和终点。", hintEn: "Press and drag to set the line start and end points." },
  { value: "arrow", icon: "→", labelZh: "箭头", labelEn: "Arrow", key: "A", hintZh: "支持直线、直角和圆弧箭头，端点可重新拖动。", hintEn: "Supports straight, orthogonal, and arc arrows with editable endpoints." },
];

const EDITOR_STATUS_EN: Record<string, string> = {
  "正在准备 React SVG 工作区…": "Preparing the React SVG workspace…",
  "正在把整张图转换为 React 可编辑 SVG…": "Converting the full image to editable React SVG…",
  "已加载底图；旧编辑记录不可读，本次从干净画布继续。": "Base image loaded; old edit records were unreadable, so this session starts from a clean canvas.",
  "没有可撤回的操作。": "There is nothing to undo.",
  "已撤回上一步。": "Undid the previous action.",
  "请先选择一个或多个对象。": "Select one or more objects first.",
  "未选择对象。": "No object selected.",
  "橡皮擦操作已加入；请确认没有擦到化学结构或文字。": "Eraser operation added; verify that no chemistry structure or text was erased.",
  "对象位置已更新。": "Object position updated.",
  "框选区域内没有可编辑对象。": "No editable objects were found inside the marquee.",
  "直线已添加。": "Line added.",
  "箭头已添加；选择后可继续调整端点。": "Arrow added; select it to adjust its endpoints.",
  "端点位置已更新。": "Endpoint position updated.",
  "请先选择一个文本对象。": "Select a text object first.",
  "文字颜色和字号已应用。": "Text color and size applied.",
  "正在加载本地 Ketcher…": "Loading local Ketcher…",
  "已载入所选结构。": "Selected structure loaded.",
  "Ketcher 已就绪；绘制后插入当前 SVG 画布。": "Ketcher is ready; draw a structure and insert it into the current SVG canvas.",
  "正在导出化学结构 SVG…": "Exporting chemistry structure SVG…",
  "Ketcher 化学结构已更新。": "Ketcher chemistry structure updated.",
  "Ketcher 化学结构已插入；可直接选择并移动。": "Ketcher chemistry structure inserted and ready to select or move.",
  "正在计算当前可见内容边界…": "Calculating visible content bounds…",
  "当前内容已经贴合画布。": "The current content already fits the canvas.",
  "已恢复到当前底图的初始状态。": "Restored the initial state of the current base image.",
  "已下载当前全图 SVG。": "Downloaded the current full-image SVG.",
  "正在保存 React SVG 编辑结果…": "Saving React SVG edits…",
  "SVG 和 PNG 已保存，正在刷新第五阶段结果。": "SVG and PNG saved; refreshing Stage 5 results.",
  "空文本已删除。": "Empty text deleted.",
  "文本内容和样式已更新。": "Text content and style updated.",
  "空文本框已取消。": "Empty text box cancelled.",
  "文本已插入；可切换到选择工具移动。": "Text inserted; switch to Select to move it.",
  "服务器没有返回全图 SVG。": "The server did not return a full-image SVG.",
  "无法将当前 SVG 转换为 PNG。": "Unable to convert the current SVG to PNG.",
  "浏览器无法创建图像画布。": "The browser could not create an image canvas.",
  "Ketcher 未返回可插入的 SVG。": "Ketcher did not return an insertable SVG.",
  "Ketcher SVG 格式无效。": "The Ketcher SVG is invalid.",
  "Ketcher 初始化超时。": "Ketcher initialization timed out.",
  "Ketcher 尚未完成初始化。": "Ketcher has not finished initializing.",
  "无法读取当前 SVG 进行裁剪。": "Unable to read the current SVG for cropping.",
  "浏览器无法读取 SVG 像素。": "The browser could not read SVG pixels.",
  "画布中没有可裁剪的可见内容。": "The canvas has no visible content to crop.",
};

function localizeEditorStatus(value: string, english: boolean): string {
  if (!english) return value;
  if (EDITOR_STATUS_EN[value]) return EDITOR_STATUS_EN[value];
  let match = /^已删除 (\d+) 个对象。$/.exec(value);
  if (match) return `Deleted ${match[1]} objects.`;
  match = /^已框选 (\d+) 个对象。$/.exec(value);
  if (match) return `Selected ${match[1]} objects.`;
  match = /^画布已裁剪为 (.+)；可撤回或保存。$/.exec(value);
  if (match) return `Canvas cropped to ${match[1]}; undo or save the result.`;
  match = /^React SVG 工作区已就绪；底图：(.+)；保存分辨率 (.+)。$/.exec(value);
  if (match) return `React SVG workspace ready; base image: ${match[1] === "AI 重绘图" ? "AI redraw" : "source"}; save resolution ${match[2]}.`;
  return value;
}

function identifier(prefix: string): string {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function isInputTarget(value: EventTarget | null): boolean {
  return value instanceof HTMLInputElement
    || value instanceof HTMLTextAreaElement
    || value instanceof HTMLSelectElement
    || (value instanceof HTMLElement && value.isContentEditable);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function pointDistance(start: Point, end: Point): number {
  return Math.hypot(end.x - start.x, end.y - start.y);
}

function normalizeBox(start: Point, end: Point): CropBox {
  return {
    x: Math.min(start.x, end.x),
    y: Math.min(start.y, end.y),
    width: Math.abs(end.x - start.x),
    height: Math.abs(end.y - start.y),
  };
}

function materializeTextDraft(
  state: SvgEditorState,
  draft: TextDraft,
  color: string,
  fontSize: number,
): TextMaterialization {
  const content = draft.text;
  if (draft.existingId) {
    if (!content.trim()) {
      return {
        state: { ...state, elements: state.elements.filter((item) => item.id !== draft.existingId) },
        selection: [],
        message: "空文本已删除。",
        changed: true,
      };
    }
    return {
      state: {
        ...state,
        elements: state.elements.map((item) => item.id === draft.existingId && item.type === "text"
          ? { ...item, text: content, color, fontSize }
          : item),
      },
      selection: [`el:${draft.existingId}`],
      message: "文本内容和样式已更新。",
      changed: true,
    };
  }
  if (!content.trim()) {
    return { state, selection: [], message: "空文本框已取消。", changed: false };
  }
  const id = identifier("text");
  const element: TextElement = {
    id,
    type: "text",
    x: draft.x,
    y: draft.y + fontSize,
    text: content,
    color,
    fontSize,
  };
  return {
    state: { ...state, elements: [...state.elements, element] },
    selection: [`el:${id}`],
    message: "文本已插入；可切换到选择工具移动。",
    changed: true,
  };
}

async function svgToPng(svg: string, width: number, height: number): Promise<string> {
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const node = new Image();
      node.onload = () => resolve(node);
      node.onerror = () => reject(new Error("无法将当前 SVG 转换为 PNG。"));
      node.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width));
    canvas.height = Math.max(1, Math.round(height));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器无法创建图像画布。");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/png");
  } finally {
    URL.revokeObjectURL(url);
  }
}

function svgNumber(value: string | null | undefined, fallback = 0): number {
  const match = String(value || "").trim().match(/^-?[\d.]+/);
  const parsed = match ? Number(match[0]) : Number.NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export async function generatedSvg(
  image: Blob | string | { text: () => Promise<string> },
  maxWidth = 320,
  maxHeight = 220,
): Promise<GeneratedKetcherSvg> {
  const blobLike = image && typeof image !== "string" && typeof image.text === "function";
  let markup = blobLike ? await image.text() : String(image || "");
  if (markup.startsWith("data:image/svg+xml")) {
    const payload = markup.slice(markup.indexOf(",") + 1);
    markup = /;base64,/i.test(markup) ? atob(payload) : decodeURIComponent(payload);
  }
  if (!/<svg[\s>]/i.test(markup)) {
    try { markup = atob(markup); } catch { /* The provider may already return text. */ }
  }
  if (!/<svg[\s>]/i.test(markup)) throw new Error("Ketcher 未返回可插入的 SVG。");
  const parsed = new DOMParser().parseFromString(markup, "image/svg+xml");
  if (parsed.documentElement.nodeName.toLowerCase() !== "svg" || parsed.querySelector("parsererror")) {
    throw new Error("Ketcher SVG 格式无效。");
  }
  const root = parsed.documentElement;
  const rawViewBox = (root.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
  const sourceWidth = rawViewBox.length === 4 && Number.isFinite(rawViewBox[2]) && rawViewBox[2] > 0
    ? rawViewBox[2]
    : svgNumber(root.getAttribute("width"), 300);
  const sourceHeight = rawViewBox.length === 4 && Number.isFinite(rawViewBox[3]) && rawViewBox[3] > 0
    ? rawViewBox[3]
    : svgNumber(root.getAttribute("height"), 180);
  if (!(rawViewBox.length === 4 && rawViewBox.every(Number.isFinite))) {
    root.setAttribute("viewBox", `0 0 ${sourceWidth} ${sourceHeight}`);
  }
  const scale = Math.min(
    Math.max(1, maxWidth) / sourceWidth,
    Math.max(1, maxHeight) / sourceHeight,
  );
  const width = Math.max(24, sourceWidth * scale);
  const height = Math.max(24, sourceHeight * scale);
  root.removeAttribute("x");
  root.removeAttribute("y");
  root.setAttribute("width", String(width));
  root.setAttribute("height", String(height));
  root.setAttribute("preserveAspectRatio", "xMinYMin meet");
  root.setAttribute("overflow", "visible");
  root.setAttribute("data-ketcher-render", "true");
  return { markup: new XMLSerializer().serializeToString(root), width, height };
}

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
            next = mergeSavedSvg(next, saved, audit.operations);
          } catch {
            setStatus({ text: "已加载底图；旧编辑记录不可读，本次从干净画布继续。", error: true });
          }
        }
        if (cancelled) return;
        setModel(next);
        setStatus({
          text: `React SVG 工作区已就绪；底图：${baseMode === "redrawn" ? "AI 重绘图" : "原图"}；保存分辨率 ${next.sourceWidth}×${next.sourceHeight}。`,
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

  const displaySvg = useMemo(() => model ? buildSvgDocument(model, {
    interactive: true,
    selection,
    transientErase,
  }) : "", [model, selection, transientErase]);

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
        node.style.translate = `${delta.x}px ${delta.y}px`;
      });
    });
  }, []);

  const applyMove = useCallback((state: SvgEditorState, keys: string[], delta: Point): SvgEditorState => {
    const selected = new Set(keys);
    const selectedTraceIds = keys.filter((key) => key.startsWith("trace:")).map((key) => key.slice(6));
    const traceEdits = new Map(state.traceEdits.map((item) => [item.id, item]));
    selectedTraceIds.forEach((id) => {
      const current = traceEdits.get(id);
      traceEdits.set(id, {
        id,
        dx: (current?.dx || 0) + delta.x,
        dy: (current?.dy || 0) + delta.y,
        hidden: current?.hidden,
      });
    });
    return {
      ...state,
      traceEdits: [...traceEdits.values()],
      operations: state.operations.map((item) => selected.has(`op:${item.id}`) ? moveOperation(item, delta.x, delta.y) : item),
      elements: state.elements.map((item) => selected.has(`el:${item.id}`) ? { ...item, x: item.x + delta.x, y: item.y + delta.y } : item),
    };
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
    const selectedNode = target.closest<SVGElement>("[data-select-key]");
    const key = selectedNode?.getAttribute("data-select-key") || "";
    if (tool === "text" && key.startsWith("el:")) {
      const element = model.elements.find((item) => `el:${item.id}` === key);
      if (element?.type === "text") {
        event.preventDefault();
        openTextEditor(element);
        return;
      }
    }
    if (tool === "select" && handle) {
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
      pushHistory(model);
      pointerRef.current = { kind: "move", start: current, selection: nextSelection };
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
      previewMove(session.selection, { x: current.x - session.start.x, y: current.y - session.start.y });
    } else if (session.kind === "handle") {
      setModel({ ...model, operations: model.operations.map((item) => item.id === session.id ? updateHandle(item, session.handle, current) : item) });
    } else if (session.kind === "marquee" || session.kind === "text") {
      showMarquee(normalizeBox(session.start, current));
    }
  }, [canvasPointAt, model, previewMove, showMarquee]);

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
        setModel({ ...model, operations: [...model.operations, { id: identifier("erase"), type: "erase", points: session.points, color: "#ffffff", width: eraseWidth, coordinateSpace: "source" }] });
        setDirty(true);
        setStatus({ text: "橡皮擦操作已加入；请确认没有擦到化学结构或文字。" });
      }
      setTransientErase([]);
    } else if (session.kind === "move") {
      const delta = { x: current.x - session.start.x, y: current.y - session.start.y };
      if (Math.hypot(delta.x, delta.y) > 0.5) {
        setModel(applyMove(model, session.selection, delta));
        setDirty(true);
        setStatus({ text: "对象位置已更新。" });
      } else {
        previewMove(session.selection, { x: 0, y: 0 });
      }
    } else if (session.kind === "marquee") {
      const left = Math.min(session.clientStart.x, event.clientX);
      const right = Math.max(session.clientStart.x, event.clientX);
      const top = Math.min(session.clientStart.y, event.clientY);
      const bottom = Math.max(session.clientStart.y, event.clientY);
      const nodes = [...(canvasRef.current?.querySelectorAll<SVGElement>("[data-select-key]") || [])]
        .filter((node) => {
          const bounds = node.getBoundingClientRect();
          return (bounds.width > 0 || bounds.height > 0)
            && bounds.right >= left && bounds.left <= right
            && bounds.bottom >= top && bounds.top <= bottom;
        })
        .map((node) => node.getAttribute("data-select-key") || "")
        .filter(Boolean);
      setSelection([...new Set(nodes)]);
      setStatus({ text: nodes.length ? `已框选 ${new Set(nodes).size} 个对象。` : "框选区域内没有可编辑对象。" });
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
      const generated = await generatedSvg(
        await api.generateImage(ket, { outputFormat: "svg" }),
        Math.max(24, model.crop.width * .38),
        Math.max(24, model.crop.height * .38),
      );
      pushHistory(model);
      if (ketcherTarget) {
        setModel({ ...model, elements: model.elements.map((item) => item.id === ketcherTarget && item.type === "ketcher" ? { ...item, ket, svgMarkup: generated.markup } : item) });
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
        };
        setModel({ ...model, elements: [...model.elements, element] });
        setSelection([`el:${id}`]);
        setStatus({ text: "Ketcher 化学结构已插入；可直接选择并移动。" });
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
          <section><h3>{text("化学结构", "Chemical structure")}</h3><button className="button button-secondary" type="button" onClick={() => openKetcher(null)}>{text("Ketcher 添加结构", "Add structure with Ketcher")}</button><button className="button button-secondary" type="button" disabled={!selectedKetcher} onClick={() => selectedKetcher && openKetcher(selectedKetcher.id)}>{text("编辑所选结构", "Edit selected structure")}</button></section>
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
