import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest } from "../../api/client";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

type StorageReport = {
  available: boolean; busy?: boolean; cooldown?: boolean;
  disk?: { total_bytes: number; free_bytes: number; used_bytes: number; low_space: boolean };
  usage?: { total_bytes: number; categories: Record<string, number>; partial: boolean; scanned_at: string };
  last_cleanup?: { at: string; removed_directories: number; freed_bytes: number; errors: number; error_reasons?: string[]; partial: boolean };
};
const bytes = (n: number) => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`;

export function StorageMaintenance({ active }: { active: boolean }) {
  const { text } = useUiText();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["admin-storage"], enabled: active,
    queryFn: () => apiRequest<StorageReport>("/api/v1/admin/storage"), refetchInterval: active ? 60000 : false });
  const cleanup = useMutation({ mutationFn: () => apiRequest<StorageReport>("/api/v1/admin/storage/cleanup", { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["admin-storage"] }) });
  const report = query.data;
  const errorReasons: Record<string, string> = {
    permission_denied: text("文件权限不足", "File permission denied"),
    files_changed: text("处理期间文件发生变化", "Files changed during cleanup"),
    filesystem_error: text("文件系统读写失败", "Filesystem read/write failure"),
  };
  const categories = [
    ["library", text("原始论文与文献库", "Papers and library")],
    ["images", text("项目图片", "Project images")],
    ["exports", text("导出文件", "Exports")],
    ["temporary", text("暂存文件（不一定可清理）", "Staging files (not all removable)")],
    ["indexes", text("向量索引", "Vector indexes")],
    ["manuscripts_and_other", text("正文历史及其他", "Manuscripts, history and other")],
  ];
  return <section className="surface admin-storage-panel">
    <div className="pane-head"><div><h2>{text("存储与安全清理", "Storage and safe cleanup")}</h2>
      <p className="muted">{text("仅自动清理可安全移除的任务暂存文件，保留论文、正文历史、图片和导出成果。", "Only safe task staging files are cleaned. Papers, manuscript history, images and exports are retained.")}</p></div>
      <button className="button button-secondary" disabled={!report?.available || cleanup.isPending} onClick={() => cleanup.mutate()}>
        {cleanup.isPending ? text("正在检查并清理…", "Checking and cleaning…") : text("清理安全缓存", "Clean safe cache")}</button>
    </div>
    {query.error || cleanup.error ? <ErrorState error={query.error || cleanup.error} onRetry={() => query.refetch()} /> : null}
    {query.isPending ? <p role="status">{text("正在读取存储信息…", "Loading storage information…")}</p> : null}
    {report?.available === false ? <p>{text("当前运行模式不提供存储维护。", "Storage maintenance is unavailable in this mode.")}</p> : null}
    {report?.disk ? <>
      {report.disk.low_space ? <p role="alert" className="message message-warning">{text("存储空间不足：剩余低于 5 GB 或总容量的 10%。请管理员检查；系统不会自动删除用户成果。", "Low disk space: less than 5 GB or 10% remains. Administrator attention is needed; user outputs will not be deleted automatically.")}</p> : null}
      <div className="admin-overview-grid">
        <article><span>{text("所在文件系统已用", "Filesystem used")}</span><strong>{bytes(report.disk.used_bytes)}</strong></article>
        <article><span>{text("所在文件系统剩余", "Filesystem free")}</span><strong>{bytes(report.disk.free_bytes)}</strong></article>
        <article><span>{text("已统计工作区文件", "Scanned workspace files")}</span><strong>{report.usage ? bytes(report.usage.total_bytes) : "—"}</strong></article>
      </div>
      <p className="muted">{text("容量为工作区所在文件系统的视图，可能包含其他数据；下方分类只统计用户工作区，不含数据库和 Docker 镜像、容器日志。", "Capacity describes the workspace filesystem and may include unrelated data. Categories cover user workspaces only, excluding the database, Docker images and container logs.")}</p>
    </> : null}
    {report?.usage ? <>
      <div className="admin-overview-grid">{categories.map(([key, label]) => <article key={key}><span>{label}</span><strong>{bytes(report.usage!.categories[key] || 0)}</strong></article>)}</div>
      <p className="muted">{text("统计时间：", "Scanned: ")}{new Date(report.usage.scanned_at).toLocaleString()}</p>
      {report.usage.partial ? <p className="message message-warning">{text("本次扫描未完整完成，显示的是部分统计，不代表全部占用。", "This scan is partial; displayed totals do not cover all files.")}</p> : null}
    </> : report?.available ? <p>{text("等待后台首次统计；也可点击安全清理执行检查。", "Waiting for the first maintenance scan; safe cleanup also triggers a scan.")}</p> : null}
    {report?.last_cleanup ? <p role="status">{text("上次清理：", "Last cleanup: ")}{new Date(report.last_cleanup.at).toLocaleString()} · {text("释放 ", "Freed ")}{bytes(report.last_cleanup.freed_bytes)} · {report.last_cleanup.removed_directories} {text("个暂存目录", "staging directories")}
      {report.last_cleanup.errors ? text(`；${report.last_cleanup.errors} 个目录未完整清理：`, `; ${report.last_cleanup.errors} directories not fully cleaned: `) + (report.last_cleanup.error_reasons || []).map(reason => errorReasons[reason] || errorReasons.filesystem_error).join(" / ") : ""}
      {report.last_cleanup.partial ? text("；本轮达到处理上限，下次继续检查。", "; pass limit reached, remaining paths will be checked later.") : ""}</p> : null}
    {cleanup.isSuccess ? <p role="status">{cleanup.data.busy ? text("已有清理任务正在执行，请稍后刷新。", "Maintenance is already running. Refresh later.") : cleanup.data.cooldown ? text("刚刚已执行检查，无需重复清理。", "Maintenance ran recently; no repeat needed.") : text("安全检查已完成。", "Safe maintenance completed.")}</p> : null}
    <p className="muted">{text("每小时检查一次；只清理成功完成超过 48 小时且无引用、无新写入的任务暂存目录。失败、取消、未知目录及待恢复任务均保留。", "Checked hourly. Only unreferenced successful-job staging older than 48 hours with no recent writes is removed. Failed, cancelled, unknown and recoverable tasks are retained.")}</p>
  </section>;
}
