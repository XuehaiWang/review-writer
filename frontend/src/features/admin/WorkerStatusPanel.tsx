import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

type Queue = "scientific" | "model" | "image" | "ingest" | "document" | "bibliography";
type Worker = { worker_id: string; status: string; queues?: Queue[]; capacity?: number;
  active_jobs?: number; waiting_model_jobs?: number; updated_at?: string };
type Status = { paused_queues: Record<Queue, boolean>;
  workers: Worker[]; queue_counts: Record<string, Record<string, number>> };
const queues: { id: Queue; zh: string; en: string }[] = [
  { id: "scientific", zh: "科研写作", en: "Scientific writing" },
  { id: "model", zh: "文本模型", en: "Text models" },
  { id: "image", zh: "图片", en: "Images" },
  { id: "ingest", zh: "文献导入", en: "Ingestion" },
  { id: "document", zh: "文档出版", en: "Documents" },
  { id: "bibliography", zh: "书目核验", en: "Bibliography" },
];

export function WorkerStatusPanel({ active }: { active: boolean }) {
  const { text } = useUiText();
  const client = useQueryClient();
  const key = ["admin", "workers"];
  const query = useQuery({ queryKey: key, queryFn: () => apiRequest<Status>("/api/v1/admin/workers"),
    enabled: active, refetchInterval: active ? 15_000 : false });
  const update = useMutation({
    mutationFn: ({ queue, paused }: { queue: Queue; paused: boolean }) =>
      apiRequest(`/api/v1/admin/workers/queues/${queue}`, { method: "PUT", ...jsonBody({ paused }) }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: key }); },
  });

  return <section className="surface admin-workers-panel">
    <div className="section-heading"><div>
      <span className="step-label">{text("后台任务", "Background tasks")}</span>
      <h2>{text("Worker 运行状态", "Worker status")}</h2>
      <p className="muted">{text("暂停接单只影响新任务；当前任务会继续完成。", "Pausing intake affects new jobs only; running jobs continue.")}</p>
    </div></div>
    {query.error ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}
    {query.isPending ? <p role="status">{text("正在读取任务状态…", "Loading worker status…")}</p> : null}
    {query.data ? <>
      <div className="admin-worker-queues">{queues.map(({ id, zh, en }) => {
        const paused = query.data!.paused_queues[id];
        const counts = query.data!.queue_counts[id] || {};
        return <div key={id} className="admin-worker-queue">
          <strong>{text(zh, en)}</strong>
          <span>{text(`排队 ${counts.queued || 0} · 运行 ${counts.running || 0}`,
            `Queued ${counts.queued || 0} · Running ${counts.running || 0}`)}
            {counts.retry_waiting ? text(` · 等待重试 ${counts.retry_waiting}`,
              ` · Retry wait ${counts.retry_waiting}`) : null}</span>
          <button type="button" className="button button-quiet" disabled={update.isPending}
            onClick={() => update.mutate({ queue: id, paused: !paused })}>
            {paused ? text("恢复接单", "Resume intake") : text("暂停接单", "Pause intake")}
          </button>
        </div>;
      })}</div>
      <details><summary>{text(`查看 Worker 实例（${query.data.workers.length}）`,
        `View worker instances (${query.data.workers.length})`)}</summary>
        <div className="admin-worker-instances">{query.data.workers.map(worker => {
          const age = worker.updated_at ? Date.now() - new Date(worker.updated_at).getTime() : Infinity;
          const online = worker.status === "running" && age < 45_000;
          return <div key={worker.worker_id}>
            <strong>{online ? text("在线", "Online") : text("离线或失联", "Offline or stale")}</strong>
            <span>{(worker.queues || []).join(", ")} · {text("本地执行", "Local work")}
              {Math.max(0, (worker.active_jobs || 0) - (worker.waiting_model_jobs || 0))}/{worker.capacity || 0}
              {worker.waiting_model_jobs ? text(` · 等模型 ${worker.waiting_model_jobs}`,
                ` · Waiting on model ${worker.waiting_model_jobs}`) : null}</span>
            <small>{worker.updated_at ? new Date(worker.updated_at).toLocaleString() : "—"}</small>
          </div>;
        })}</div>
      </details>
    </> : null}
    {update.error ? <ErrorState error={update.error} /> : null}
  </section>;
}
