from threading import Barrier, Event, Lock, get_ident
from review_writer_core.stages.sections.execution import run_sections
from review_writer_core.model_gateway_client import DeferredModelCall
import pytest


def test_two_workers_finish_independently_and_coordinator_owns_events():
    barrier = Barrier(2)
    coordinator = get_ident()
    saved, phases = [], []
    def generate(task, emit):
        assert get_ident() != coordinator
        barrier.wait(timeout=3)
        emit("reviewing")
        return {"output": task["section_id"]}
    def observe(task, phase):
        assert get_ident() == coordinator
        phases.append((task["section_id"], phase))
    def save(task, result):
        assert get_ident() == coordinator
        saved.append(task["section_id"])
    run_sections([{"section_id": "A"}, {"section_id": "B"}], generate, observe, save)
    assert set(saved) == {"A", "B"}
    assert ("A", "reviewing") in phases and ("B", "reviewing") in phases


def test_dependency_failure_does_not_block_independent_chapter_or_replay_cached():
    called, saved = [], {}
    tasks = [{"section_id": "cached"}, {"section_id": "A"},
        {"section_id": "B", "depends_on_sections": ["A"]}, {"section_id": "C"}]
    def generate(task, emit):
        called.append(task["section_id"])
        if task["section_id"] == "A":
            raise RuntimeError("Unavailable")
        return {"writing": {"claims": []}}
    run_sections(tasks, generate, lambda *_: None, lambda t, r: saved.update({t["section_id"]: r}), completed={"cached": {}})
    assert set(called) == {"A", "C"}
    assert "error" in saved["A"] and "B" not in saved and "error" not in saved["C"]


def test_dependency_runs_after_saved_predecessor_and_receives_context():
    saved = []
    def generate(task, emit):
        if task["section_id"] == "B":
            assert saved == ["A"]
            assert task["dependency_context"][0]["claims"] == ["verified"]
        return {"writing": {"claims": ["verified"]}}
    run_sections([{"section_id": "A"}, {"section_id": "B", "depends_on_sections": ["A"]}],
        generate, lambda *_: None, lambda t, r: saved.append(t["section_id"]))
    assert saved == ["A", "B"]


def test_single_slot_refills_dependencies_without_repeating_work():
    tasks = [{"section_id": "A"}, {"section_id": "B", "depends_on_sections": ["A"]}]
    completed, called = {}, []

    def generate(task, emit):
        called.append(task["section_id"])
        if task["section_id"] == "B":
            assert task["dependency_context"][0]["claims"] == ["from A"]
        return {"writing": {"claims": ["from A"]}}

    observe = lambda *_: None
    save = lambda task, result: completed.update({task["section_id"]: result})
    assert not run_sections(tasks, generate, observe, save, completed=completed, concurrency=1)
    assert called == ["A", "B"]


def test_failed_chapter_can_be_deferred_while_independent_chapters_continue():
    tasks = [{"section_id": "A"}, {"section_id": "B", "depends_on_sections": ["A"]},
             {"section_id": "C"}]
    completed, failed, called = {}, set(), []

    def generate(task, emit):
        sid = task["section_id"]
        called.append(sid)
        return {"error": "provider unavailable"} if sid == "A" else {"writing": {"claims": []}}

    def save(task, result):
        sid = task["section_id"]
        if "error" in result:
            failed.add(sid)
        else:
            completed[sid] = result

    assert run_sections(tasks, generate, lambda *_: None, save,
                        completed=completed, deferred=failed, concurrency=1) is False
    assert called == ["A", "C"]
    assert "C" in completed and failed == {"A"}


def test_model_delegation_exits_chapter_pool_without_recording_failed_prose():
    saved = []
    def generate(_task, _emit):
        raise DeferredModelCall("child")
    with pytest.raises(DeferredModelCall) as deferred:
        run_sections([{"section_id": "A"}], generate, lambda *_: None,
                     lambda task, _result: saved.append(task["section_id"]), concurrency=1)
    assert deferred.value.model_job_id == "child"
    assert saved == []


def test_fifth_chapter_fills_freed_slot_before_slow_siblings_finish():
    barrier = Barrier(4)
    refilled = Event()
    saved = []
    def generate(task, _emit):
        if task["section_id"] != "4":
            barrier.wait(timeout=3)
            if task["section_id"] != "0":
                assert refilled.wait(timeout=3)
        else:
            refilled.set()
        return {"output": task["section_id"]}
    tasks = [{"section_id": str(i)} for i in range(5)]
    assert not run_sections(tasks, generate, lambda *_: None,
                        lambda t, r: saved.append(t["section_id"]), concurrency=4)
    assert refilled.is_set()
    assert set(saved) == {"0", "1", "2", "3", "4"}


def test_delegated_chapter_does_not_discard_parallel_completed_result():
    barrier, observed = Barrier(2), Event()
    saved = []
    def generate(task, emit):
        barrier.wait(timeout=3)
        if task["section_id"] == "A":
            emit("waiting")
            raise DeferredModelCall("child")
        assert observed.wait(timeout=3)
        return {"output": "completed"}
    def observe(_task, phase):
        if phase == "waiting":
            observed.set()
    with pytest.raises(DeferredModelCall):
        run_sections([{"section_id": "A"}, {"section_id": "B"}], generate, observe,
                     lambda t, r: saved.append((t["section_id"], r)), concurrency=4)
    assert saved == [("B", {"output": "completed"})]


def test_model_waits_release_local_slots_and_all_ready_children_are_retained():
    barrier = Barrier(2)
    called = []
    def generate(task, _emit):
        called.append(task["section_id"])
        if task["section_id"] in {"0", "1"}:
            barrier.wait(timeout=3)
        raise DeferredModelCall("child-" + task["section_id"])
    with pytest.raises(DeferredModelCall) as caught:
        run_sections([{"section_id": str(i)} for i in range(7)], generate,
                     lambda *_: None, lambda *_: pytest.fail("Not completed"), concurrency=2)
    assert set(called) == {str(i) for i in range(7)}
    assert set(caught.value.model_job_ids) == {"child-" + str(i) for i in range(7)}


def test_waiting_predecessor_blocks_only_its_dependents():
    called = []
    def generate(task, _emit):
        called.append(task["section_id"])
        raise DeferredModelCall("child-" + task["section_id"])
    tasks = [{"section_id": "A"}, {"section_id": "B", "depends_on_sections": ["A"]},
             {"section_id": "C"}, {"section_id": "D", "depends_on_sections": ["B"]}]
    with pytest.raises(DeferredModelCall) as caught:
        run_sections(tasks, generate, lambda *_: None,
                     lambda *_: pytest.fail("Not completed"), concurrency=1)
    assert called == ["A", "C"]
    assert set(caught.value.model_job_ids) == {"child-A", "child-C"}


def test_local_work_stays_bounded_while_model_waits_accumulate():
    barrier, lock = Barrier(2), Lock()
    active = peak = 0
    def generate(task, _emit):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=3)
            raise DeferredModelCall("child-" + task["section_id"])
        finally:
            with lock:
                active -= 1
    with pytest.raises(DeferredModelCall) as caught:
        run_sections([{"section_id": str(i)} for i in range(8)], generate,
                     lambda *_: None, lambda *_: pytest.fail("Not completed"), concurrency=2)
    assert peak == 2 and active == 0
    assert len(caught.value.model_job_ids) == 8


def test_resume_saves_ready_chapter_and_unlocks_dependency_despite_other_waits():
    called, saved = [], {}
    def generate(task, _emit):
        sid = task["section_id"]
        called.append(sid)
        if sid == "A":
            raise DeferredModelCall("existing-A")
        if sid == "C":
            assert "B" in saved
            assert task["dependency_context"][0]["claims"] == ["verified"]
            raise DeferredModelCall("new-C")
        return {"writing": {"claims": ["verified"]}}
    tasks = [{"section_id": "cached"}, {"section_id": "A"}, {"section_id": "B"},
             {"section_id": "C", "depends_on_sections": ["B"]}]
    with pytest.raises(DeferredModelCall) as caught:
        run_sections(tasks, generate, lambda *_: None,
                     lambda t, r: saved.update({t["section_id"]: r}),
                     completed={"cached": {}}, concurrency=1)
    assert called == ["A", "B", "C"]
    assert set(saved) == {"B"}
    assert set(caught.value.model_job_ids) == {"existing-A", "new-C"}
