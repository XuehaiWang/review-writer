from __future__ import annotations

import concurrent.futures
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
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
        self.runner = runner(
            max_attempts=3,
            retry_delay_seconds=0,
            poll_interval=0.02,
            allow_private_networks=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_connection_resolver_rejects_private_redirect_and_rebinding_targets(self) -> None:
        from review_writer_api.scientific_entrypoint import guarded_getaddrinfo

        private_answer = [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]
        guarded = guarded_getaddrinfo(
            lambda *_args, **_kwargs: private_answer,
            allow_private_networks=False,
        )
        with self.assertRaisesRegex(OSError, "private"):
            guarded("provider.example", 443)

        public_answer = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]
        guarded = guarded_getaddrinfo(
            lambda *_args, **_kwargs: public_answer,
            allow_private_networks=False,
        )
        self.assertEqual(public_answer, guarded("provider.example", 443))

    def test_connection_resolver_allows_only_configured_proxy_networks(self) -> None:
        from review_writer_api.scientific_entrypoint import guarded_getaddrinfo

        proxy_answer = [(2, 1, 6, "", ("198.18.0.74", 443))]
        guarded = guarded_getaddrinfo(
            lambda *_args, **_kwargs: proxy_answer,
            allow_private_networks=False,
            trusted_proxy_networks=("198.18.0.0/15",),
        )
        self.assertEqual(proxy_answer, guarded("mineru.net", 443))

        lan_answer = [(2, 1, 6, "", ("192.168.0.9", 443))]
        guarded = guarded_getaddrinfo(
            lambda *_args, **_kwargs: lan_answer,
            allow_private_networks=False,
            trusted_proxy_networks=("198.18.0.0/15",),
        )
        with self.assertRaisesRegex(OSError, "private"):
            guarded("internal.example", 443)

    def test_connection_resolver_allows_only_the_bound_internal_gateway_port(self) -> None:
        from review_writer_api.scientific_entrypoint import guarded_getaddrinfo

        loopback_answer = [(2, 1, 6, "", ("127.0.0.1", 8770))]
        guarded = guarded_getaddrinfo(
            lambda *_args, **_kwargs: loopback_answer,
            allow_private_networks=False,
            allowed_private_endpoint=("127.0.0.1", 8770),
        )
        self.assertEqual(loopback_answer, guarded("127.0.0.1", 8770))
        with self.assertRaisesRegex(OSError, "private"):
            guarded("127.0.0.1", 9000)

    def test_runner_enables_connection_time_network_guard_by_default(self) -> None:
        _output, _cancelled, _failed, runner = runner_api()
        guarded_runner = runner(max_attempts=1, retry_delay_seconds=0)
        with patch("review_writer_api.scientific_runner.subprocess.Popen") as popen:
            popen.side_effect = OSError("stop after environment capture")
            with self.assertRaises(WorkflowValidationError):
                guarded_runner.run(
                    [sys.executable, "-c", "pass"],
                    cwd=self.root,
                    staging_directory=self.root,
                    expected_outputs=("done.txt",),
                )
        child_environment = popen.call_args.kwargs["env"]
        self.assertEqual("0", child_environment["REVIEW_WRITER_ALLOW_PRIVATE_EGRESS"])

    def test_runner_passes_explicit_proxy_networks_to_child(self) -> None:
        _output, _cancelled, _failed, runner = runner_api()
        guarded_runner = runner(
            max_attempts=1,
            retry_delay_seconds=0,
            trusted_proxy_networks=("198.18.0.0/15", "fdfe:dcba:9876::/64"),
        )
        with patch("review_writer_api.scientific_runner.subprocess.Popen") as popen:
            popen.side_effect = OSError("stop after environment capture")
            with self.assertRaises(WorkflowValidationError):
                guarded_runner.run(
                    [sys.executable, "-c", "pass"],
                    cwd=self.root,
                    staging_directory=self.root,
                    expected_outputs=("done.txt",),
                )
        child_environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            "198.18.0.0/15,fdfe:dcba:9876::/64",
            child_environment["REVIEW_WRITER_TRUSTED_PROXY_NETWORKS"],
        )

    def test_runner_binds_private_gateway_exception_to_scoped_token_endpoint(self) -> None:
        _output, _cancelled, _failed, runner = runner_api()
        guarded_runner = runner(max_attempts=1, retry_delay_seconds=0)
        with patch("review_writer_api.scientific_runner.subprocess.Popen") as popen:
            popen.side_effect = OSError("stop after environment capture")
            with self.assertRaises(WorkflowValidationError):
                guarded_runner.run(
                    [sys.executable, "-c", "pass"],
                    cwd=self.root,
                    staging_directory=self.root,
                    expected_outputs=("done.txt",),
                    env={
                        "REVIEW_WRITER_MODEL_GATEWAY_URL": (
                            "http://127.0.0.1:8770/api/internal/v1/model-responses"
                        )
                    },
                    secret_env={"REVIEW_WRITER_TASK_TOKEN": "scoped-token"},
                )
        child_environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            "127.0.0.1:8770",
            child_environment["REVIEW_WRITER_INTERNAL_GATEWAY_ENDPOINT"],
        )

    def test_runner_can_import_application_modules_outside_repository_cwd(self) -> None:
        _output, _cancelled, _failed, runner = runner_api()
        isolated_runner = runner(max_attempts=1, retry_delay_seconds=0)
        result = isolated_runner.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "import review_writer_api.scientific_tasks; "
                    "Path('module-imported.txt').write_text('ok', encoding='utf-8')"
                ),
            ],
            cwd=self.root,
            staging_directory=self.root,
            expected_outputs=("module-imported.txt",),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("ok", (self.root / "module-imported.txt").read_text())

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

    def test_non_transient_failure_exposes_a_redacted_actionable_last_line(self) -> None:
        secret = "sk-diagnostic-secret"
        script = (
            "import os,sys; "
            "sys.stderr.write('Traceback (most recent call last):\\n'); "
            "sys.stderr.write('RuntimeError: Candidate integrity failed for '+"
            "os.environ['TASK_ONLY_SECRET']+'\\n'); "
            "sys.exit(1)"
        )
        with self.assertRaises(self.RunFailed) as failed:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("never-created.txt",),
                secret_env={"TASK_ONLY_SECRET": secret},
            )

        self.assertEqual(1, failed.exception.attempts)
        self.assertIn(
            "Scientific task failed: Candidate integrity failed for [REDACTED]",
            str(failed.exception),
        )
        self.assertNotIn(secret, str(failed.exception))

    def test_transient_503_retries_twice_then_succeeds(self) -> None:
        script = (
            "import json,pathlib,sys; p=pathlib.Path('attempt.txt'); "
            "n=int(p.read_text() if p.exists() else '0')+1; p.write_text(str(n)); "
            "sys.stderr.write('REVIEW_WRITER_ERROR:'+json.dumps({'category':'transient_service_unavailable','provider_call_completed':False})+'\\n') if n < 3 else pathlib.Path('done.txt').write_text('ok'); "
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

    def test_urllib_provider_errors_are_structured_by_the_production_adapter(self) -> None:
        class Provider(http.server.BaseHTTPRequestHandler):
            attempts = 0

            def do_POST(self):
                type(self).attempts += 1
                if type(self).attempts < 3:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"error":"temporarily unavailable"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        script = self.root / "provider_script.py"
        script.write_text(
            "import pathlib,sys,urllib.request\n"
            "request=urllib.request.Request(sys.argv[1], data=b'{}', method='POST')\n"
            "with urllib.request.urlopen(request, timeout=2) as response:\n"
            "    response.read()\n"
            "pathlib.Path('provider-output.json').write_text('{\"ok\":true}')\n",
            encoding="utf-8",
        )
        try:
            result = self.runner.run(
                [
                    sys.executable,
                    str(script),
                    f"http://127.0.0.1:{server.server_port}/v1/responses",
                ],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("provider-output.json",),
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(3, result.attempts)
        self.assertEqual(3, Provider.attempts)
        self.assertEqual(
            {"ok": True},
            json.loads((self.root / "provider-output.json").read_text()),
        )

    def test_cloudflare_524_is_retried_by_the_production_adapter(self) -> None:
        class Provider(http.server.BaseHTTPRequestHandler):
            attempts = 0

            def do_POST(self):
                type(self).attempts += 1
                if type(self).attempts < 3:
                    self.send_response(524)
                    self.end_headers()
                    self.wfile.write(b'{"error":"origin response timeout"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        script = self.root / "provider_524_script.py"
        script.write_text(
            "import pathlib,sys,urllib.request\n"
            "request=urllib.request.Request(sys.argv[1], data=b'{}', method='POST')\n"
            "with urllib.request.urlopen(request, timeout=2) as response:\n"
            "    response.read()\n"
            "pathlib.Path('provider-524-output.json').write_text('{\"ok\":true}')\n",
            encoding="utf-8",
        )
        try:
            result = self.runner.run(
                [
                    sys.executable,
                    str(script),
                    f"http://127.0.0.1:{server.server_port}/v1/responses",
                ],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("provider-524-output.json",),
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(3, result.attempts)
        self.assertEqual(3, Provider.attempts)

    def test_exhausted_524_reports_an_actionable_timeout(self) -> None:
        script = (
            "import json,sys; "
            "sys.stderr.write('REVIEW_WRITER_ERROR:'+json.dumps({"
            "'category':'transient_service_unavailable',"
            "'provider_call_completed':False,'http_status':524})+'\\n'); "
            "sys.exit(1)"
        )
        with self.assertRaises(self.RunFailed) as failed:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("provider-timeout-output.json",),
            )

        self.assertEqual(3, failed.exception.attempts)
        self.assertTrue(failed.exception.retryable)
        self.assertEqual(524, failed.exception.details["http_status"])
        self.assertIn("timed out (HTTP 524) after 3 attempts", str(failed.exception))

    def test_real_inventory_script_runs_through_the_scientific_adapter(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        review_root = self.root / "review-root"
        project = review_root / "review-projects" / "adapter-probe"
        discovery = project / "00_discovery"
        staging = project / "02_section_drafting"
        discovery.mkdir(parents=True)
        staging.mkdir(parents=True)
        (discovery / "selected_discovery_results.json").write_text(
            '{"local_papers": []}', encoding="utf-8"
        )
        script = (
            project_root
            / "skills"
            / "review-section-drafting-figure-picking"
            / "scripts"
            / "build_paper_figure_inventory.py"
        )

        result = self.runner.run(
            [
                sys.executable,
                str(script),
                "--review-root",
                str(review_root),
                "--project-id",
                "adapter-probe",
            ],
            cwd=project_root,
            staging_directory=staging,
            expected_outputs=("paper_figure_inventory.json",),
        )

        self.assertEqual(1, result.attempts)
        inventory = json.loads(
            (staging / "paper_figure_inventory.json").read_text()
        )
        self.assertEqual("adapter-probe", inventory["project_id"])
        self.assertEqual(0, inventory["paper_count"])

    def test_failed_attempt_output_is_not_accepted_as_a_later_success(self) -> None:
        script = (
            "import json,pathlib,sys; p=pathlib.Path('stale-attempt.txt'); "
            "n=int(p.read_text() if p.exists() else '0')+1; p.write_text(str(n)); "
            "pathlib.Path('done.txt').write_text('partial') if n == 1 else None; "
            "sys.stderr.write('REVIEW_WRITER_ERROR:'+json.dumps({'category':'transient_service_unavailable','provider_call_completed':False})+'\\n') if n == 1 else None; "
            "sys.exit(1 if n == 1 else 0)"
        )
        with self.assertRaises(self.OutputMissing) as missing:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("done.txt",),
            )
        self.assertEqual(2, missing.exception.attempts)

    def test_structured_validation_permission_and_post_call_failures_never_retry(self) -> None:
        for category, completed in (
            ("validation", False),
            ("permission", False),
            ("post_call_summary_failed", True),
        ):
            with self.subTest(category=category):
                counter = self.root / f"{category}.txt"
                script = (
                    "import json,os,pathlib,sys; "
                    f"p=pathlib.Path({str(counter)!r}); "
                    "p.write_text(str(int(p.read_text() if p.exists() else '0')+1)); "
                    + (
                        "pathlib.Path(os.environ['REVIEW_WRITER_PROVIDER_CALL_COMPLETED_FILE']).write_text('done'); "
                        if completed
                        else ""
                    )
                    + "sys.stderr.write('HTTP 503 temporary text must not control retry\\n'); "
                    + f"sys.stderr.write('REVIEW_WRITER_ERROR:'+json.dumps({{'category':{category!r},'provider_call_completed':{completed!r}}})+'\\n'); sys.exit(1)"
                )
                with self.assertRaises(self.RunFailed) as failed:
                    self.runner.run(
                        [sys.executable, "-c", script],
                        cwd=self.root,
                        staging_directory=self.root,
                        expected_outputs=(f"{category}.out",),
                    )
                self.assertEqual(1, failed.exception.attempts)
                self.assertFalse(failed.exception.retryable)
                self.assertEqual("1", counter.read_text())

    def test_partial_batch_service_failure_reports_internal_retry_exhaustion(self) -> None:
        script = (
            "import json,os,pathlib,sys; "
            "pathlib.Path(os.environ['REVIEW_WRITER_PROVIDER_CALL_COMPLETED_FILE']).write_text('done'); "
            "sys.stderr.write('batch failed with HTTP 503 after 5 provider attempts: model_not_found\\n'); "
            "sys.stderr.write('REVIEW_WRITER_ERROR:'+json.dumps({'category':'transient_service_unavailable','provider_call_completed':True,'http_status':503})+'\\n'); "
            "sys.exit(1)"
        )
        with self.assertRaises(self.RunFailed) as failed:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("partial-batch.out",),
            )

        self.assertEqual(1, failed.exception.attempts)
        self.assertFalse(failed.exception.retryable)
        message = str(failed.exception)
        self.assertIn("exhausted its internal request retries", message)
        self.assertIn("model_not_found", message)
        self.assertNotIn("after 1 attempts", message)

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
        with self.assertRaisesRegex(WorkflowValidationError, "at least one"):
            self.runner.run(
                [sys.executable, "-c", "pass"],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=(),
            )

    def test_cancellation_terminates_the_child_process(self) -> None:
        cancel = threading.Event()

        def execute():
            return self.runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("never.txt",),
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

    def test_cancellation_terminates_spawned_descendants(self) -> None:
        cancel = threading.Event()
        grandchild = (
            "import pathlib,time; time.sleep(0.8); "
            "pathlib.Path('grandchild-survived.txt').write_text('bad')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([{sys.executable!r}, '-c', {grandchild!r}]); "
            "time.sleep(30)"
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: self.runner.run(
                    [sys.executable, "-c", parent],
                    cwd=self.root,
                    staging_directory=self.root,
                    expected_outputs=("never.txt",),
                    cancel_requested=cancel.is_set,
                    timeout_seconds=10,
                )
            )
            time.sleep(0.15)
            cancel.set()
            with self.assertRaises(self.RunCancelled):
                future.result(timeout=3)
        time.sleep(1)
        self.assertFalse((self.root / "grandchild-survived.txt").exists())

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
                expected_outputs=("never.txt",),
                timeout_seconds=0.3,
            )
        self.assertEqual(3, failed.exception.attempts)
        self.assertTrue(failed.exception.retryable)
        self.assertEqual("3", (self.root / "timeouts.txt").read_text())
        self.assertIn("execution time limit after 3 task attempts", str(failed.exception))
        self.assertEqual(0.3, failed.exception.details["timeout_seconds"])

    def test_task_limit_after_a_completed_provider_call_is_not_mislabeled(self) -> None:
        script = (
            "import os,pathlib,time; "
            "pathlib.Path(os.environ['REVIEW_WRITER_PROVIDER_CALL_COMPLETED_FILE']).write_text('done'); "
            "time.sleep(30)"
        )
        with self.assertRaises(self.RunFailed) as failed:
            self.runner.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                staging_directory=self.root,
                expected_outputs=("never.txt",),
                timeout_seconds=0.3,
            )

        self.assertEqual(1, failed.exception.attempts)
        self.assertFalse(failed.exception.retryable)
        self.assertIn("execution time limit after 1 task attempt", str(failed.exception))
        self.assertNotIn("provider timed out", str(failed.exception).casefold())


if __name__ == "__main__":
    unittest.main()
