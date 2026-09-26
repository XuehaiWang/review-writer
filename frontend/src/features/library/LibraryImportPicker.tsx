import { useRef, useState } from "react";
import { useUiText } from "../../i18n/useUiText";

export function LibraryImportPicker({ busy, onFiles, onArchive }: {
  busy: boolean; onFiles: (files: File[]) => void; onArchive: (file: File) => void;
}) {
  const { text } = useUiText();
  const menu = useRef<HTMLDetailsElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [archive, setArchive] = useState<File | null>(null);
  const [ignored, setIgnored] = useState(0);
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [error, setError] = useState("");
  function select(input: FileList | null, zip = false) {
    setError(""); setExcluded(new Set()); setArchive(null); setFiles([]); setIgnored(0);
    const items = Array.from(input || []);
    if (zip) {
      const file = items[0];
      if (file && (!file.name.toLowerCase().endsWith(".zip") || file.size > 256 * 1024 * 1024)) {
        setError(text("请选择不超过 256 MB 的 ZIP 文件。", "Choose a ZIP file of at most 256 MB.")); return;
      }
      setArchive(file || null); return;
    }
    const pdfs = items.filter(file => file.name.toLowerCase().endsWith(".pdf"));
    setIgnored(items.length - pdfs.length);
    setFiles(pdfs);
    if (!pdfs.length) setError(text("没有找到 PDF 文件。", "No PDF files found."));
  }
  const selected = files.filter((_, index) => !excluded.has(index));
  const oversized = selected.some(file => file.size > 80 * 1024 * 1024);
  return <details ref={menu} className="library-import-picker">
    <summary className="button button-primary">{text("导入文献", "Import papers")}</summary>
    <div className="library-import-menu">
      <strong>{text("选择导入方式", "Choose an import source")}</strong>
      <div className="library-import-options">
        <label className="button button-secondary file-button">{text("选择 PDF", "PDF files")}<input aria-label={text("选择 PDF", "PDF files")} type="file" multiple accept=".pdf,application/pdf" disabled={busy} onChange={event => { select(event.target.files); event.currentTarget.value = ""; }} /></label>
        <label className="button button-secondary file-button">{text("选择文件夹", "Folder")}<input aria-label={text("选择文件夹", "Folder")} type="file" multiple {...{ webkitdirectory: "" }} disabled={busy} onChange={event => { select(event.target.files); event.currentTarget.value = ""; }} /></label>
        <label className="button button-secondary file-button">{text("选择 ZIP", "ZIP archive")}<input aria-label={text("选择 ZIP", "ZIP archive")} type="file" accept=".zip,application/zip" disabled={busy} onChange={event => { select(event.target.files, true); event.currentTarget.value = ""; }} /></label>
      </div>
      <small>{text("自动包含子文件夹中的 PDF；其他文件只会忽略，不会覆盖书目信息。", "Includes PDFs in subfolders. Other files are ignored; metadata is not overwritten.")}</small>
      {files.length ? <>
        <p>{text(`已选 ${selected.length} 个 PDF，共 ${(selected.reduce((sum, file) => sum + file.size, 0) / 1024 / 1024).toFixed(1)} MB；忽略 ${ignored} 个其他文件。`, `${selected.length} PDFs selected; ${(selected.reduce((sum, file) => sum + file.size, 0) / 1024 / 1024).toFixed(1)} MB; ${ignored} other files ignored.`)}</p>
        <div className="library-import-files">{files.map((file, index) => <label key={index}>
          <input type="checkbox" checked={!excluded.has(index)} disabled={busy} onChange={() => setExcluded(previous => { const next = new Set(previous); if (next.has(index)) next.delete(index); else next.add(index); return next; })} />
          <span>{file.webkitRelativePath || file.name}{file.size > 80 * 1024 * 1024 ? text("（超过 80 MB）", " (exceeds 80 MB)") : ""}</span>
        </label>)}</div>
      </> : null}
      {archive ? <p>{archive.name}<br /><small>{text("上传后在后台收集 PDF，再逐篇解析。最多 300 个 PDF、展开后 1 GB；不支持加密包和嵌套压缩包。", "PDFs are collected in the background, then parsed individually. Up to 300 PDFs and 1 GB expanded. Encrypted and nested archives are not supported.")}</small></p> : null}
      {error || oversized ? <p className="message message-error" role="alert">{error || text("请取消选择超过 80 MB 的 PDF。", "Deselect PDFs larger than 80 MB.")}</p> : null}
      <button type="button" className="button button-primary" disabled={busy || oversized || (!archive && !selected.length)} onClick={() => {
        if (archive) onArchive(archive); else onFiles(selected);
        setFiles([]); setArchive(null); if (menu.current) menu.current.open = false;
      }}>{busy ? text("正在上传…", "Uploading…") : text("开始导入", "Start import")}</button>
    </div>
  </details>;
}
