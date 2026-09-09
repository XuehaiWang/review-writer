import { ApiError, apiRequest, jsonBody } from "../../../api/client";
import {
  type BaseMode,
  type CropBox,
  type EditorTool,
  type Point,
  type SvgEditorState,
  type TextElement,
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


export type TextDraft = {
  x: number;
  y: number;
  width: number;
  height: number;
  text: string;
  existingId?: string;
};

export type TextMaterialization = {
  state: SvgEditorState;
  selection: string[];
  message: string;
  changed: boolean;
};

type GeneratedKetcherSvg = { markup: string; width: number; height: number };

export const TOOL_META: Array<{ value: EditorTool; icon: string; labelZh: string; labelEn: string; key: string; hintZh: string; hintEn: string }> = [
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
  "请先选择一个 Ketcher 结构。": "Select a Ketcher structure first.",
  "请先选择一个文本对象。": "Select a text object first.",
  "文字颜色和字号已应用。": "Text color and size applied.",
  "正在加载本地 Ketcher…": "Loading local Ketcher…",
  "已载入所选结构。": "Selected structure loaded.",
  "Ketcher 已就绪；绘制后插入当前 SVG 画布。": "Ketcher is ready; draw a structure and insert it into the current SVG canvas.",
  "正在导出化学结构 SVG…": "Exporting chemistry structure SVG…",
  "Ketcher 化学结构已更新。": "Ketcher chemistry structure updated.",
  "Ketcher 化学结构已插入；可直接选择并移动。": "Ketcher chemistry structure inserted and ready to select or move.",
  "Ketcher 化学结构已插入；可移动并拖动右下角调整大小。": "Ketcher chemistry structure inserted; move it or drag the lower-right handle to resize it.",
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

export function localizeEditorStatus(value: string, english: boolean): string {
  if (!english) return value;
  if (EDITOR_STATUS_EN[value]) return EDITOR_STATUS_EN[value];
  let match = /^已删除 (\d+) 个对象。$/.exec(value);
  if (match) return `Deleted ${match[1]} objects.`;
  match = /^已选择 (\d+) 个对象，可拖动或删除。$/.exec(value);
  if (match) return `Selected ${match[1]} objects; drag or delete them.`;
  match = /^已框选 (\d+) 个对象，可直接删除。$/.exec(value);
  if (match) return `Selected ${match[1]} objects; they are ready to delete.`;
  match = /^已框选 (\d+) 个对象。$/.exec(value);
  if (match) return `Selected ${match[1]} objects.`;
  match = /^结构尺寸已更新为 (\d+)%。$/.exec(value);
  if (match) return `Structure size updated to ${match[1]}%.`;
  match = /^画布已裁剪为 (.+)；可撤回或保存。$/.exec(value);
  if (match) return `Canvas cropped to ${match[1]}; undo or save the result.`;
  match = /^React SVG 工作区已就绪；底图：(.+)；保存分辨率 (.+)。$/.exec(value);
  if (match) return `React SVG workspace ready; base image: ${match[1] === "AI 重绘图" ? "AI redraw" : "source"}; save resolution ${match[2]}.`;
  return value;
}

export function identifier(prefix: string): string {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

export function isInputTarget(value: EventTarget | null): boolean {
  return value instanceof HTMLInputElement
    || value instanceof HTMLTextAreaElement
    || value instanceof HTMLSelectElement
    || (value instanceof HTMLElement && value.isContentEditable);
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function pointDistance(start: Point, end: Point): number {
  return Math.hypot(end.x - start.x, end.y - start.y);
}

export function normalizeBox(start: Point, end: Point): CropBox {
  return {
    x: Math.min(start.x, end.x),
    y: Math.min(start.y, end.y),
    width: Math.abs(end.x - start.x),
    height: Math.abs(end.y - start.y),
  };
}

export function clampKetcherScale(value: number): number {
  return Math.max(.1, Math.min(5, Number.isFinite(value) ? value : 1));
}

export function ketcherScaleAtPoint(
  anchor: Point,
  width: number,
  height: number,
  current: Point,
): number {
  const denominator = width * width + height * height;
  if (denominator <= 0) return 1;
  const projected = ((current.x - anchor.x) * width + (current.y - anchor.y) * height) / denominator;
  return clampKetcherScale(projected);
}

const TRACE_HIT_RADIUS = 4;
const MARQUEE_EDGE_PADDING = 2;

export function selectableKey(value: Element | null): string {
  return value?.closest<SVGElement>("[data-select-key]")?.getAttribute("data-select-key") || "";
}

/**
 * Raster-to-SVG tracing can leave text and thin bonds narrower than one CSS
 * pixel after the editor scales the figure. Keep Ketcher and inserted-element
 * hit testing unchanged, but sample a small area around a missed click so the
 * original trace remains practical to select.
 */
export function traceKeyNearClientPoint(
  root: Element,
  clientX: number,
  clientY: number,
  radius = TRACE_HIT_RADIUS,
): string {
  const documentNode = root.ownerDocument;
  if (typeof documentNode.elementsFromPoint !== "function") return "";
  const offsets = [
    [0, 0],
    [-radius / 2, 0], [radius / 2, 0], [0, -radius / 2], [0, radius / 2],
    [-radius, 0], [radius, 0], [0, -radius], [0, radius],
    [-radius, -radius], [radius, -radius], [-radius, radius], [radius, radius],
  ];
  for (const [dx, dy] of offsets) {
    for (const node of documentNode.elementsFromPoint(clientX + dx, clientY + dy)) {
      if (!root.contains(node)) continue;
      const key = selectableKey(node);
      // The tolerance is intentionally limited to the vectorized source layer.
      // Inserted text, lines and Ketcher structures retain exact hit testing.
      if (key.startsWith("trace:")) return key;
    }
  }
  return "";
}

export function selectableKeysInClientBox(
  root: Element,
  box: { left: number; right: number; top: number; bottom: number },
  padding = MARQUEE_EDGE_PADDING,
): string[] {
  const left = box.left - padding;
  const right = box.right + padding;
  const top = box.top - padding;
  const bottom = box.bottom + padding;
  const keys = [...root.querySelectorAll<SVGElement>("[data-select-key]")]
    .filter((node) => {
      const style = globalThis.getComputedStyle?.(node);
      if (style?.display === "none" || style?.visibility === "hidden") return false;
      const bounds = node.getBoundingClientRect();
      return (bounds.width > 0 || bounds.height > 0)
        && bounds.right >= left && bounds.left <= right
        && bounds.bottom >= top && bounds.top <= bottom;
    })
    .map((node) => node.getAttribute("data-select-key") || "")
    .filter(Boolean);
  return [...new Set(keys)];
}

export function materializeTextDraft(
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

export async function svgToPng(svg: string, width: number, height: number): Promise<string> {
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
