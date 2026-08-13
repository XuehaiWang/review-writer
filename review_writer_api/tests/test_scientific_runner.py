from __future__ import annotations

import concurrent.futures
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from review_writer_api.errors import WorkflowValidationError


def runner_api():
    try:
        from review_writer_api.scientific_runner import (
            ScientificOutputMissing,
            ScientificRunCancelled,
            ScientificRunFailed,
            ScientificRunner,
        )
    except ModuleNotFoundError as exc:
        raise AssertionError("The secure scientific subprocess runner is missing.") from exc
    return ScientificOutputMissing, ScientificRunCancelled, ScientificRunFailed, ScientificRunner


class ScientificRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        output_missing, run_cancelled, run_failed, runner = runner_api()
        self.OutputMissing = output_missing
        self.RunCancelled = run_cancelled
        self.RunFailed = run_failed
        self.runner = runner(max_attempts=3, retry_delay_seconds=0, poll_interval=0.02)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_secret_is_child_scoped_and_redacted_from_retained_diagnostics(self) -> None:
        secret = "sk-runner-secret"
        os.environ.pop("TASK_ONLY_SECRET", None)
        script = (
            "import os,pathlib,sys; "
            "print(os.environ['TASK_ONLY_SECRET'], file=sys.stderr); "
            "pathlib.Path('done.txt').write_text('ok', encoding='utf-8')"
        )
        result = self.runner.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            staging_directory=self.root,
            expected_outputs=("done.txt",),
            secret_env={"TASK_ONLY_SECRET": secret},
        )

        self.assertNotIn("TASK_ONLY_SECRET", os.environ)
        self.assertNotIn(secret, result.stderr)
        self.assertIn("[REDACTED]", result.stderr)
        self.assertNotIn(secret, " ".join(result.command))
        self.assertEqual(1, result.attempts)

    def test_transient_503_retries_twice_then_succeeds(self) -> None:
        script = (
            "import pathlib,sys; p=pathlib.Path('attempt.txt'); "
            "n=int(p.read_text() if p.exists() else '0')+1; p.write_text(str(n)); "
            "sys.stderr.write('HTTP 503 temporary\\n') if n < 3 else pathlib.Path('done.txt').write_text('ok'); "
            "sys.exit(1 if n < 3 else 0)"
        )
        result = self.runner.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            staging_directory=self.root,
            expected_outputs=("done.txt",),
        )

        self.assertEqual(3, result.attempts)
        self.assertEqual("3", (self.root / "attempt.txt").read_text())

    def test_missing_output_and_invalid_paths_never_retry(self) -> None:
        counter = self.root / "counter.txt"
        script = (
            "import pathlib; p=pathlib.Path('counter.txt'); "
            "p.write_text(str(int(p.read_text() if p.exists() else '0')+1))"
        )
        with self.assertRaises(self.OutputMissing) as missing:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("required.json",),
            )
        self.assertEqual(1, missing.exception.attempts)
        self.assertEqual("1", counter.read_text())

        with self.assertRaises(WorkflowValidationError):
            self.runner.run(
                [sys.executable, "-c", "pass"],
                cwd=self.root / "missing-working-directory",
                staging_directory=self.root,
                expected_outputs=(),
            )
        with self.assertRaises(WorkflowValidationError):
            self.runner.run(
                [sys.executable, "-c", "pass"],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=(),
                env={"OPENAI_API_KEY": "must-use-secret-env"},
            )

    def test_cancellation_terminates_the_child_process(self) -> None:
        cancel = threading.Event()

        def execute():
            return self.runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=(),
                cancel_requested=cancel.is_set,
                timeout_seconds=10,
            )

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(execute)
            time.sleep(0.1)
            cancel.set()
            with self.assertRaises(self.RunCancelled):
                future.result(timeout=3)
        self.assertLess(time.monotonic() - started, 3)

    def test_timeout_retries_at_most_three_attempts(self) -> None:
        script = (
            "import pathlib,time; p=pathlib.Path('timeouts.txt'); "
            "p.write_text(str(int(p.read_text() if p.exists() else '0')+1)); time.sleep(30)"
        )
        with self.assertRaises(self.RunFailed) as failed:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=(),
                timeout_seconds=0.08,
            )
        self.assertEqual(3, failed.exception.attempts)
        self.assertTrue(failed.exception.retryable)
        self.assertEqual("3", (self.root / "timeouts.txt").read_text())


if __name__ == "__main__":
    unittest.main()
