"""Bounded chapter execution. Only the calling coordinator publishes state."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue

from review_writer_core.model_gateway_client import DeferredModelCall


def chapter_responsibilities(tasks):
    """One shared snapshot for writing, audit, and checkpoint invalidation."""
    return [{key: task.get(key) for key in (
        "section_id", "heading", "writing_objective", "questions_to_answer", "avoid_points",
        "primary_papers", "supporting_papers", "paper_roles", "paragraph_tasks")}
        for task in tasks]


def run_sections(tasks, generate, observe, save, *, completed=None, deferred=None, concurrency=2):
    completed = completed or {}
    retained = dict(completed) if isinstance(completed, dict) else {}
    deferred = set(deferred or ())
    pending = {t["section_id"]: t for t in tasks
               if t["section_id"] not in completed and t["section_id"] not in deferred}
    done, running, failed = set(completed), set(), set()
    capacity = max(1, int(concurrency))
    events = Queue()
    model_waits = []

    def work(task):
        sid = task["section_id"]
        try:
            result = generate(deepcopy(task), lambda phase: events.put(("phase", sid, phase)))
        except DeferredModelCall as exc:
            events.put(("deferred", sid, exc))
            return
        except Exception as exc:
            result = {"error": str(exc)[:2000]}
        events.put(("result", sid, result))

    with ThreadPoolExecutor(max_workers=capacity) as pool:
        while pending or running:
            for sid, task in list(pending.items()):
                if len(running) + len(model_waits) >= capacity:
                    break
                dependencies = task.get("depends_on_sections") or []
                if not set(dependencies).issubset(done):
                    continue
                del pending[sid]
                running.add(sid)
                observe(task, "preparing")
                worker_task = deepcopy(task)
                worker_task["dependency_context"] = [{"section_id": dependency,
                    "paragraphs": (retained.get(dependency, {}).get("writing") or {}).get("paragraphs", []),
                    "claims": (retained.get(dependency, {}).get("writing") or {}).get("claims", [])}
                    for dependency in dependencies]
                pool.submit(work, worker_task)
            if not running:
                if model_waits:
                    raise DeferredModelCall(model_waits[0], model_job_ids=model_waits)
                blocked = deferred | failed
                while True:
                    newly_blocked = {
                        sid for sid, task in pending.items()
                        if set(task.get("depends_on_sections") or []).intersection(blocked)
                    }
                    if newly_blocked.issubset(blocked):
                        break
                    blocked.update(newly_blocked)
                for sid, task in pending.items():
                    if sid in blocked:
                        continue
                    save(task, {"error": "章节依赖尚未完成，请先恢复前置章节。"})
                break
            kind, sid, value = events.get()
            task = next(t for t in tasks if t["section_id"] == sid)
            if kind == "phase":
                observe(task, value)
            elif kind == "deferred":
                # Drain the other in-flight chapters and persist their results
                # before yielding the lease to delegated model jobs.
                running.remove(sid)
                deferred.add(sid)
                model_waits.extend(value.model_job_ids)
            else:
                running.remove(sid)
                save(task, value)
                if "error" in value:
                    failed.add(sid)
                else:
                    done.add(sid)
                    retained[sid] = value
    if model_waits:
        raise DeferredModelCall(model_waits[0], model_job_ids=model_waits)
    return False
