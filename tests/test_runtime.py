import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import runtime_server as runtime
from scripts import refresh_dashboard_from_wecom as source


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.data_patch = patch.object(runtime, "DATA", self.directory)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.dashboard = runtime.Dashboard(self.directory / "app.sqlite3")

    def test_success_survives_restart_and_updates_embedded_html(self):
        updated = copy.deepcopy(self.dashboard.payload)
        updated["meta"]["snapshotAt"] = "2026-10-22 09:20:00"
        with patch.object(runtime, "fetch_records", return_value=[{}]), patch.object(runtime, "build_payload", return_value=updated):
            self.dashboard.refresh()
        restored = runtime.Dashboard(self.dashboard.db)
        self.assertEqual(restored.payload, updated)
        self.assertIn('"snapshotAt":"2026-10-22 09:20:00"', restored.html())
        self.assertEqual(restored.status()["status"], "success")

    def test_failure_retains_last_successful_snapshot(self):
        original = copy.deepcopy(self.dashboard.payload)
        with patch.object(runtime, "fetch_records", side_effect=RuntimeError("private diagnostic")):
            self.dashboard.refresh()
        self.assertEqual(self.dashboard.payload, original)
        self.assertEqual(runtime.Dashboard(self.dashboard.db).payload, original)
        self.assertEqual(self.dashboard.status()["status"], "error")
        self.assertNotIn("private diagnostic", json.dumps(self.dashboard.status()))

    def test_concurrent_refresh_joins_existing_job(self):
        entered, release = threading.Event(), threading.Event()
        def read(_):
            entered.set()
            release.wait(5)
            return []
        with patch.object(runtime, "fetch_records", side_effect=read):
            self.assertTrue(self.dashboard.start())
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.dashboard.start())
            run = self.dashboard.status()["runId"]
            self.assertEqual(self.dashboard.status()["runId"], run)
            release.set()
            self.dashboard.worker.join(2)
            self.assertFalse(self.dashboard.worker.is_alive())

    def test_interrupted_job_recovers_with_previous_snapshot(self):
        self.dashboard.state = {"status": "running", "runId": "test-run"}
        self.dashboard.persist()
        restored = runtime.Dashboard(self.dashboard.db)
        self.assertEqual(restored.status()["status"], "error")
        self.assertEqual(restored.payload, self.dashboard.payload)

    def test_schedule_rolls_over_at_midnight_in_shanghai(self):
        current = datetime(2026, 10, 22, 23, 59, 59, tzinfo=runtime.SHANGHAI)
        self.assertEqual(runtime.next_hour(current).isoformat(), "2026-10-23T00:00:00+08:00")


class PaginationTests(unittest.TestCase):
    config = {"source_url": "https://example.com/smartsheet/test", "sheet_name": "业务需求收集表"}

    def test_reads_next_cursor_without_has_more(self):
        pages = [{"total": 2, "records": [{"record_id": "a"}], "next_cursor": "next"},
                 {"total": 2, "records": [{"record_id": "b"}]}]
        with patch.object(source, "cli_call", side_effect=pages) as call:
            self.assertEqual(len(source.fetch_records(self.config)), 2)
            self.assertEqual(call.call_args_list[1].args[1]["cursor"], "next")

    def test_rejects_partial_duplicate_and_changing_total(self):
        cases = [[{"total": 2, "records": [{"record_id": "a"}]}],
                 [{"total": 2, "records": [{"record_id": "a"}, {"record_id": "a"}]}],
                 [{"total": 2, "records": [{"record_id": "a"}], "next_cursor": "next"},
                  {"total": 3, "records": [{"record_id": "b"}]}]]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(source, "cli_call", side_effect=pages):
                with self.assertRaises(RuntimeError):
                    source.fetch_records(self.config)


if __name__ == "__main__":
    unittest.main()
