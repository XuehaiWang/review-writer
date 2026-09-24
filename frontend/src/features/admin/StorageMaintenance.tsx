import { uiLocale } from "../../i18n/locale";
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
  const diskPercent = report?.disk && report.disk.total_bytes > 0
    ? Math.min(100, Math.max(0, report.disk.used_bytes / report.disk.total_bytes * 100)) : 0;
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
    <header className="admin-storage-heading"><div><span className="step-label">{text("存储", "Storage")}</span><h2>{text("存储与安全清理", "Storage and safe cleanup")}</h2>
      <p className="muted">{text("仅自动清理可安全移除的任务暂存文件，保留论文、正文历史、图片和导出成果。", "Only safe task staging files are cleaned. Papers, manuscript history, images and exports are retained.")}</p></div>
      <button type="button" className="button button-primary" disabled={!report?.available || cleanup.isPending} onClick={() => cleanup.mutate()}>
        {cleanup.isPending ? text("正在检查并清理…", "Checking and cleaning…") : text("清理安全缓存", "Clean safe cache")}</button>
    </header>
    {query.error || cleanup.error ? <ErrorState error={query.error || cleanup.error} onRetry={() => query.refetch()} /> : null}
    {query.isPending ? <p role="status">{text("正在读取存储信息…", "Loading storage information…")}</p> : null}
    {report?.available === false ? <p>{text("当前运行模式不提供存储维护。", "Storage maintenance is unavailable in this mode.")}</p> : null}
    {report?.disk ? <>
      {report.disk.low_space ? <p role="alert" className="message message-warning">{text("存储空间不足：剩余低于 5 GB 或总容量的 10%。请管理员检查；系统不会自动删除用户成果。", "Low disk space: less than 5 GB or 10% remains. Administrator attention is needed; user outputs will not be deleted automatically.")}</p> : null}
      <div className="admin-storage-metrics">
        <article><span>{text("所在文件系统已用", "Filesystem used")}</span><strong>{bytes(report.disk.used_bytes)}</strong></article>
        <article><span>{text("所在文件系统剩余", "Filesystem free")}</span><strong>{bytes(report.disk.free_bytes)}</strong></article>
        <article><span>{text("已统计工作区文件", "Scanned workspace files")}</span><strong>{report.usage ? bytes(report.usage.total_bytes) : "—"}</strong></article>
      </div>
      <div className={`admin-storage-capacity${report.disk.low_space ? " is-low" : ""}`}>
        <div><span>{text("文件系统使用率", "Filesystem utilization")}</span><strong>{diskPercent.toFixed(1)}% <small>/ {bytes(report.disk.total_bytes)}</small></strong></div>
        <div className="admin-storage-meter" role="meter" aria-label={text("文件系统使用率", "Filesystem utilization")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Number(diskPercent.toFixed(1))}><span style={{ width: `${diskPercent}%` }} /></div>
      </div>
      <p className="muted admin-storage-note">{text("容量为工作区所在文件系统的视图，可能包含其他数据；下方分类只统计用户工作区，不含数据库和 Docker 镜像、容器日志。", "Capacity describes the workspace filesystem and may include unrelated data. Categories cover user workspaces only, excluding the database, Docker images and container logs.")}</p>
    </> : null}
    <div className="admin-storage-body">
    <section className="admin-storage-breakdown" aria-label={text("工作区文件分类", "Workspace breakdown")}>
      <h3>{text("工作区文件分类", "Workspace breakdown")}</h3>
    {report?.usage ? <>
      <dl className="admin-storage-categories">{categories.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{bytes(report.usage!.categories[key] || 0)}</dd></div>)}</dl>
      <p className="muted admin-storage-note">{text("统计时间：", "Scanned: ")}{new Date(report.usage.scanned_at).toLocaleString(uiLocale())}</p>
      {report.usage.partial ? <p className="message message-warning">{text("本次扫描未完整完成，显示的是部分统计，不代表全部占用。", "This scan is partial; displayed totals do not cover all files.")}</p> : null}
    </> : report?.available ? <p>{text("等待后台首次统计；也可点击安全清理执行检查。", "Waiting for the first maintenance scan; safe cleanup also triggers a scan.")}</p> : null}
    </section>
    <section className="admin-storage-maintenance" aria-label={text("清理记录与规则", "Cleanup history and policy")}>
      <h3>{text("最近一次清理", "Latest cleanup")}</h3>
      {report?.last_cleanup ? <>
        <p className="muted admin-storage-note">{new Date(report.last_cleanup.at).toLocaleString(uiLocale())}</p>
        <dl className="admin-storage-cleanup-stats">
          <div><dt>{text("释放空间", "Space freed")}</dt><dd>{bytes(report.last_cleanup.freed_bytes)}</dd></div>
          <div><dt>{text("移除暂存目录", "Staging directories removed")}</dt><dd>{report.last_cleanup.removed_directories}</dd></div>
        </dl>
        {report.last_cleanup.errors ? <p className="message message-warning">{text(`${report.last_cleanup.errors} 个目录未完整清理：`, `${report.last_cleanup.errors} directories not fully cleaned: `)}{(report.last_cleanup.error_reasons || []).map(reason => errorReasons[reason] || errorReasons.filesystem_error).join(" / ")}</p> : null}
        {report.last_cleanup.partial ? <p className="muted">{text("本轮达到处理上限，下次继续检查。", "Pass limit reached; remaining paths will be checked later.")}</p> : null}
      </> : <p className="muted">{text("暂无清理记录", "No cleanup history yet")}</p>}
      <div className="admin-storage-policy"><h4>{text("自动维护 · 每小时检查", "Automatic maintenance · hourly")}</h4>
        <p>{text("只清理成功完成超过 48 小时且无引用、无新写入的任务暂存目录。", "Only unreferenced successful-job staging older than 48 hours with no recent writes is removed.")}</p>
        <p className="muted">{text("失败、取消、未知目录及待恢复任务均保留。暂存占用不等于可释放空间。", "Failed, cancelled, unknown and recoverable tasks are retained. Staging usage is not the amount that can be freed.")}</p>
      </div>
    </section>
    </div>
    {cleanup.isSuccess ? <p className="admin-storage-feedback" role="status">{cleanup.data.busy ? text("已有清理任务正在执行，请稍后刷新。", "Maintenance is already running. Refresh later.") : cleanup.data.cooldown ? text("刚刚已执行检查，无需重复清理。", "Maintenance ran recently; no repeat needed.") : text("安全检查已完成。", "Safe maintenance completed.")}</p> : null}
  </section>;
}
