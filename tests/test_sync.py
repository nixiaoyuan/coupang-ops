"""Offline orchestration tests; no real login, network, or DingTalk messages."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coupang-ops" / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC = importlib.util.spec_from_file_location("coupang_sync_under_test", SCRIPTS / "sync.py")
    sync = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(sync)
    import dingtalk_notify
finally:
    sys.path.pop(0)


def dataset(store, analysis_day="2026-10-12"):
    return {
        "store": store, "timezone": "Asia/Seoul", "currency": "KRW", "analysis_date": analysis_day,
        "observed_at": "2026-10-12T10:00:00+09:00", "sales_basis": "platform_reported",
        "sales_basis_note": "平台值，未确认扣减定义", "sales_windows": {
            name: {"start_date": start, "end_date": end, "complete": True,
                   "rows": [{"sku": "SKU-1", "name": "商品", "units": units, "amount": str(units * 100), "traffic": {}}]}
            for name, start, end, units in (
                ("last_30d", "2026-09-12", "2026-10-11", 50),
                ("current_7d", "2026-10-05", "2026-10-11", 10),
                ("previous_7d", "2026-09-28", "2026-10-04", 20))},
    }


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="coupang-sync-test-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "outputs"
        self.output.mkdir()
        self.args = argparse.Namespace(output_dir=str(self.output), analysis_date="2026-10-12", extras_dir="", notify=False, render=False)
        self.config = {"stores": [{"store": "STORE-A", "cookie": "FAKE_AUTH_DO_NOT_LOG"},
                                  {"store": "STORE-B", "cookie": "FAKE_AUTH_DO_NOT_LOG"}]}
        self.stdout = io.StringIO()

    def run_refresh(self, fetch, notify=None):
        with patch.object(sync, "fetch_store", side_effect=fetch), \
             patch.object(sync, "build_workbook", return_value={"status": "updated"}), \
             patch.object(sync.shutil, "disk_usage", return_value=argparse.Namespace(free=1024 * 1024 * 1024)), \
             patch.object(sync, "notify", side_effect=notify) as notice, redirect_stdout(self.stdout):
            result = sync.perform_refresh(self.args, self.config)
        return result, notice

    def read(self, filename):
        return json.loads((self.output / filename).read_text(encoding="utf-8"))

    def test_one_failed_store_retains_old_snapshot_and_marks_aggregate_stale(self):
        old = dataset("STORE-B")
        sync.atomic_json(sync.cache_path(self.output, "STORE-B"), old)
        result, notice = self.run_refresh([dataset("STORE-A"), sync.AuthError("Authentication expired.")])
        self.assertEqual(result, 2)
        self.assertEqual(notice.call_count, 0)
        datasets = self.read("datasets.json")["datasets"]
        by_store = {item["store"]: item for item in datasets}
        self.assertEqual(by_store["STORE-A"]["refresh_status"], "success")
        self.assertEqual(by_store["STORE-B"]["refresh_status"], "stale")
        self.assertEqual(by_store["STORE-B"]["sales_windows"], old["sales_windows"])
        self.assertEqual(self.read("status.json")["stores"][1]["status"], "auth_required")
        old_summary = next(item for item in self.read("summary.json")["stores"] if item["store"] == "STORE-B")
        self.assertIn("本次采集失败", old_summary["sales_basis_note"])
        for key in ("ranking", "sales_drop", "stock_risk", "slow_stock", "returns"):
            self.assertEqual(old_summary[key]["status"], "incomplete")
            self.assertTrue(any("本次采集失败" in item for item in old_summary[key]["missing"]))
            for row in old_summary[key]["rows"]:
                self.assertIsNone(row["alert"])
                self.assertIn("historical_alert", row)
        self.assertTrue(old_summary["sales_drop"]["rows"][0]["historical_alert"])
        self.assertIn("stale", (self.output / "report.md").read_text(encoding="utf-8"))
        self.assertNotIn("FAKE_AUTH_DO_NOT_LOG", self.stdout.getvalue())

    def test_database_receives_fresh_and_stale_datasets_without_treating_failure_as_success(self):
        self.config["mysql_enabled"] = True
        self.config["dingtalk_erp_path"] = "FAKE_ERP_PATH"
        sync.atomic_json(sync.cache_path(self.output, "STORE-B"), dataset("STORE-B"))
        with patch.object(sync, "persist_refresh", return_value={"status": "updated", "rows_written": 4, "stores": 2}) as persist:
            result, _ = self.run_refresh([dataset("STORE-A"), sync.AuthError("Authentication expired.")])
        self.assertEqual(result, 2)
        erp, snapshots, states, observed_at = persist.call_args.args
        self.assertEqual(erp, "FAKE_ERP_PATH")
        self.assertEqual([item["refresh_status"] for item in snapshots], ["success", "stale"])
        self.assertEqual([item["fetch_status"] for item in states], ["success", "auth_required"])
        self.assertEqual(self.read("status.json")["database"]["rows_written"], 4)

    def test_database_write_failure_makes_refresh_fail_and_is_available_to_notice(self):
        self.args.save_db = True
        self.args.notify = True
        def notice(config, output, state):
            self.assertEqual(state["database"]["status"], "failed")
            return {"status": "delivered"}
        with patch.object(sync, "persist_refresh", return_value={"status": "failed", "reason": "database_write_failed"}):
            result, _ = self.run_refresh([dataset("STORE-A"), dataset("STORE-B")], notice)
        self.assertEqual(result, 2)
        self.assertEqual(self.read("status.json")["notification"]["status"], "delivered")

    def test_low_disk_records_only_remote_failure_and_notifies_without_overwriting_snapshots(self):
        self.args.save_db = True
        self.args.notify = True
        old = {"datasets": [dataset("STORE-A")]}
        sync.atomic_json(self.output / "datasets.json", old)
        with patch.object(sync.shutil, "disk_usage", return_value=argparse.Namespace(free=1)), \
             patch.object(sync, "fetch_store") as fetch, \
             patch.object(sync, "persist_refresh", return_value={"status": "updated", "rows_written": 2}) as persist, \
             patch.object(sync, "notify", return_value={"status": "delivered"}) as notice, redirect_stdout(self.stdout):
            result = sync.perform_refresh(self.args, self.config)
        self.assertEqual(result, 2)
        fetch.assert_not_called()
        self.assertEqual(persist.call_args.args[1], [])
        self.assertEqual([entry["fetch_status"] for entry in persist.call_args.args[2]], ["fetch_failed", "fetch_failed"])
        notice.assert_called_once()
        self.assertEqual(self.read("datasets.json"), old)
        self.assertEqual(self.read("status.json")["workbook"]["status"], "not_updated")

    def test_demo_never_persists_to_database_even_when_config_requests_it(self):
        self.config["mysql_enabled"] = True
        with patch.object(sync.shutil, "disk_usage", return_value=argparse.Namespace(free=1024 * 1024 * 1024)), \
             patch.object(sync, "build_workbook", return_value={"status": "updated"}), \
             patch.object(sync, "persist_refresh") as persist, redirect_stdout(self.stdout):
            result = sync.perform_refresh(self.args, self.config, [dataset("STORE-A")])
        self.assertEqual(result, 0)
        persist.assert_not_called()

    def test_skip_workbook_still_saves_snapshots_and_reports(self):
        self.args.skip_workbook = True
        with patch.object(sync, "fetch_store", side_effect=[dataset("STORE-A"), dataset("STORE-B")]), \
             patch.object(sync.shutil, "disk_usage", return_value=argparse.Namespace(free=1024 * 1024 * 1024)), \
             patch.object(sync, "build_workbook") as workbook, redirect_stdout(self.stdout):
            result = sync.perform_refresh(self.args, self.config)
        self.assertEqual(result, 0)
        workbook.assert_not_called()
        self.assertEqual(self.read("status.json")["workbook"]["status"], "not_requested")
        self.assertTrue((self.output / "report.md").is_file())

    def test_incomplete_remote_window_cannot_mix_with_old_pages_as_new_success(self):
        sync.atomic_json(sync.cache_path(self.output, "STORE-A"), dataset("STORE-A"))
        result, _ = self.run_refresh([sync.WingError("Pagination totals changed."), dataset("STORE-B")])
        self.assertEqual(result, 2)
        rows = self.read("datasets.json")["datasets"]
        by_store = {item["store"]: item for item in rows}
        self.assertEqual(by_store["STORE-A"]["refresh_status"], "stale")
        self.assertEqual(by_store["STORE-B"]["refresh_status"], "success")
        self.assertEqual({window["complete"] for window in by_store["STORE-A"]["sales_windows"].values()}, {True})
        self.assertEqual(self.read("status.json")["stores"][0]["status"], "fetch_failed")

    def test_failed_first_run_preserves_explicit_store_failure_without_invented_rows(self):
        result, _ = self.run_refresh([sync.AuthError("Authentication expired."), dataset("STORE-B")])
        self.assertEqual(result, 2)
        self.assertEqual([entry["store"] for entry in self.read("datasets.json")["datasets"]], ["STORE-B"])
        self.assertEqual(self.read("status.json")["stores"][0]["store"], "STORE-A")
        self.assertEqual(self.read("status.json")["stores"][0]["status"], "auth_required")

    def test_no_retry_or_loop_in_one_refresh_and_one_requested_notice(self):
        self.args.notify = True
        accepted = {"status": "accepted", "task_id": 9, "delivery": {"status": "pending"}}
        result, notice = self.run_refresh([dataset("STORE-A"), dataset("STORE-B")], lambda *args: accepted)
        self.assertEqual(notice.call_count, 1)
        self.assertEqual(self.read("status.json")["notification"], accepted)
        self.assertEqual(len(self.read("refresh-history.json")), 1)
        self.assertFalse((self.output / ".refresh.lock").exists())
        self.assertEqual(result, 0, "pending remains accepted, never claimed delivered")

    def test_configuration_reordering_preserves_each_failed_stores_cache(self):
        sync.atomic_json(sync.cache_path(self.output, "STORE-A"), dataset("STORE-A"))
        sync.atomic_json(sync.cache_path(self.output, "STORE-B"), dataset("STORE-B"))
        self.config["stores"].reverse()
        result, _ = self.run_refresh([sync.AuthError("Expired."), dataset("STORE-A")])
        self.assertEqual(result, 2)
        by_store = {item["store"]: item for item in self.read("datasets.json")["datasets"]}
        self.assertEqual(by_store["STORE-B"]["refresh_status"], "stale")
        self.assertEqual(by_store["STORE-A"]["refresh_status"], "success")
        self.assertEqual(self.read("status.json")["stores"][0]["store"], "STORE-B")

    def test_bad_cache_json_does_not_stop_other_store_or_leak_content(self):
        sync.cache_path(self.output, "STORE-A").write_text('{"cookie":"FAKE_AUTH_DO_NOT_LOG",', encoding="utf-8")
        result, _ = self.run_refresh([sync.AuthError("Expired."), dataset("STORE-B")])
        self.assertEqual(result, 2)
        self.assertEqual(self.read("status.json")["stores"][0]["cache_status"], "invalid")
        self.assertEqual([item["store"] for item in self.read("summary.json")["stores"]], ["STORE-B"])
        self.assertNotIn("FAKE_AUTH_DO_NOT_LOG", self.stdout.getvalue())

    def test_partial_old_cache_is_not_treated_as_complete_previous_success(self):
        old = dataset("STORE-A")
        old["sales_windows"]["last_30d"]["complete"] = False
        sync.atomic_json(sync.cache_path(self.output, "STORE-A"), old)
        result, _ = self.run_refresh([sync.WingError("Partial page."), dataset("STORE-B")])
        self.assertEqual(result, 2)
        self.assertEqual(self.read("status.json")["stores"][0]["cache_status"], "invalid")
        self.assertEqual([item["store"] for item in self.read("datasets.json")["datasets"]], ["STORE-B"])

    def test_bad_extras_are_per_store_failures_with_one_status_entry(self):
        extras = Path(self.temporary.name) / "private" / "extras"
        extras.mkdir(parents=True)
        (extras / "STORE-A.json").write_text("{broken FAKE_AUTH_DO_NOT_LOG", encoding="utf-8")
        self.args.extras_dir = str(extras)
        result, _ = self.run_refresh([dataset("STORE-A"), dataset("STORE-B")])
        self.assertEqual(result, 2)
        entries = self.read("status.json")["stores"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["status"], "analysis_failed")
        self.assertEqual(entries[0]["fetch_status"], "success")
        self.assertEqual(entries[1]["status"], "success")
        self.assertEqual([item["store"] for item in self.read("summary.json")["stores"]], ["STORE-B"])
        self.assertNotIn("FAKE_AUTH_DO_NOT_LOG", self.stdout.getvalue())

    def test_requested_notice_failure_is_failure_exit_and_never_retried(self):
        self.args.notify = True
        for outcome in ({"status": "missing_config"}, {"status": "failed"}, {"status": "unknown"},
                        {"status": "accepted", "task_id": 9, "delivery": {"status": "failed"}},
                        {"status": "accepted", "task_id": 9, "delivery": {"status": "unknown"}}):
            with self.subTest(outcome=outcome):
                result, notice = self.run_refresh([dataset("STORE-A"), dataset("STORE-B")], lambda *args: outcome)
                self.assertEqual(result, 2)
                self.assertEqual(notice.call_count, 1)
                self.assertEqual(self.read("status.json")["notification"], outcome)

    def test_init_requires_private_parent_after_resolving_traversal(self):
        for path in (Path(self.temporary.name) / "private", Path(self.temporary.name) / "not-private" / "config.json",
                     Path(self.temporary.name) / "private" / ".." / "public.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sync.private_config_path(path)
        valid = Path(self.temporary.name) / "private" / "nested" / "config.json"
        self.assertEqual(sync.private_config_path(valid), valid.resolve())

    def test_extras_path_cannot_traverse_or_use_windows_device_names(self):
        root = Path(self.temporary.name) / "private" / "extras"
        for store in ("../outside", "..\\outside", "C:\\outside", "/outside", "CON", "NUL.json", "store "):
            with self.subTest(store=store), self.assertRaises(ValueError):
                sync.extras_path(root, store)
        self.assertEqual(sync.extras_path(root, "STORE-A"), root.resolve() / "STORE-A.json")

    def test_notify_submits_once_and_checks_delivery_once_without_real_network(self):
        state = {"updated_at": "2026-10-12T10:00:00+09:00", "stores": [{"store": "STORE-A", "status": "success"}], "workbook": {"status": "updated"}}
        private = {"status": "configured", "app_key": "DUMMY_KEY", "app_secret": "DUMMY_SECRET", "agent_id": 1, "user_ids": ["dummy-person"]}
        with patch.object(sync, "notice_config", return_value=private), \
             patch.object(dingtalk_notify, "send_work_notice", return_value={"status": "accepted", "task_id": 123}) as send, \
             patch.object(dingtalk_notify, "verify_delivery", return_value={"status": "pending"}) as verify:
            result = sync.notify({}, self.output, state)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["delivery"]["status"], "pending")
        self.assertNotIn("DUMMY_SECRET", send.call_args.args[2])
        self.assertNotIn("cookie", send.call_args.args[2].lower())

    def test_notify_missing_recipient_does_not_attempt_network(self):
        with patch.object(sync, "notice_config", return_value={"status": "missing_config", "missing": ["user_ids"]}), \
             patch.object(dingtalk_notify, "send_work_notice") as send:
            result = sync.notify({}, self.output, {})
        self.assertEqual(result["status"], "missing_config")
        send.assert_not_called()

    def test_verification_exception_retains_accepted_task_without_resubmitting(self):
        state = {"updated_at": "2026-10-12T10:00:00+09:00", "stores": [], "workbook": {"status": "updated"}}
        with patch.object(sync, "notice_config", return_value={"status": "configured"}), \
             patch.object(dingtalk_notify, "send_work_notice", return_value={"status": "accepted", "task_id": 123}) as send, \
             patch.object(dingtalk_notify, "verify_delivery", side_effect=RuntimeError("FAKE_SECRET")) as verify:
            result = sync.notify({}, self.output, state)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["task_id"], 123)
        self.assertEqual(result["delivery"]["status"], "unknown")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(verify.call_count, 1)
        self.assertNotIn("FAKE_SECRET", json.dumps(result))

    def test_existing_lock_stops_before_fetch_and_is_not_removed(self):
        (self.output / ".refresh.lock").write_text("known-pid", encoding="ascii")
        with patch.object(sync, "fetch_store") as fetch, self.assertRaises(ValueError):
            sync.perform_refresh(self.args, self.config)
        fetch.assert_not_called()
        self.assertTrue((self.output / ".refresh.lock").exists())

    def test_dead_legacy_lock_is_recovered_and_new_lock_records_process_identity(self):
        path = self.output / ".refresh.lock"
        path.write_text("555", encoding="ascii")
        with patch.object(sync, "process_state", side_effect=[{"status": "dead"}, {"status": "alive", "process_start": 111}]) as inspect:
            with sync.run_lock(self.output):
                self.assertEqual(json.loads(path.read_text()), {"pid": os.getpid(), "process_start": 111})
            self.assertEqual(inspect.call_args_list[0].args, (555,))
        self.assertFalse(path.exists())

    def test_real_child_crash_releases_kernel_guard_and_recovers_orphaned_json_lock(self):
        code = "import os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import sync\nwith sync.run_lock(Path(sys.argv[2])):\n os._exit(0)\n"
        subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(self.output)],
                       check=True, capture_output=True, timeout=15)
        path = self.output / ".refresh.lock"
        orphan = sync.lock_owner(path)
        self.assertNotEqual(orphan["pid"], os.getpid())
        with sync.run_lock(self.output):
            self.assertEqual(sync.lock_owner(path)["pid"], os.getpid())
        self.assertFalse(path.exists())

    def test_reused_pid_with_different_creation_time_is_recovered(self):
        path = self.output / ".refresh.lock"
        path.write_text('{"pid":555,"process_start":100}', encoding="ascii")
        with patch.object(sync, "process_state", side_effect=[{"status": "alive", "process_start": 200}, {"status": "alive", "process_start": 111}]):
            with sync.run_lock(self.output):
                self.assertEqual(json.loads(path.read_text())["process_start"], 111)
        self.assertFalse(path.exists())

    def test_alive_legacy_owner_cannot_be_removed_without_original_start_time(self):
        path = self.output / ".refresh.lock"
        path.write_text("555", encoding="ascii")
        with patch.object(sync, "process_state", return_value={"status": "alive", "process_start": 200}), self.assertRaises(ValueError):
            with sync.run_lock(self.output):
                self.fail("an alive legacy PID must keep its lock")
        self.assertEqual(path.read_text(), "555")

    def test_alive_same_identity_and_unknown_permission_keep_original_lock(self):
        path = self.output / ".refresh.lock"
        payload = '{"pid":555,"process_start":100}'
        for state in ({"status": "alive", "process_start": 100}, {"status": "unknown"}):
            with self.subTest(state=state):
                path.write_text(payload, encoding="ascii")
                with patch.object(sync, "process_state", return_value=state), self.assertRaises(ValueError):
                    with sync.run_lock(self.output):
                        self.fail("uncertain owner must keep its lock")
                self.assertEqual(path.read_text(), payload)

    def test_malformed_lock_is_retained_before_any_process_inspection(self):
        path = self.output / ".refresh.lock"
        for payload in ("", "not-a-pid", "0", "-1", '{"pid":true,"process_start":100}',
                        '{"pid":555,"process_start":null}', '{"pid":555,"pid":556,"process_start":100}',
                        '{"pid":555,"process_start":100,"unknown":1}'):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="ascii")
                with patch.object(sync, "process_state") as inspect, self.assertRaises(ValueError):
                    with sync.run_lock(self.output):
                        self.fail("malformed owner must be preserved")
                inspect.assert_not_called()
                self.assertEqual(path.read_text(), payload)

    def test_live_guard_blocks_competing_run_without_deleting_owner(self):
        with sync.run_lock(self.output):
            first = (self.output / ".refresh.lock").read_bytes()
            with self.assertRaises(ValueError):
                with sync.run_lock(self.output):
                    self.fail("a competing run cannot recover an active guard")
            self.assertEqual((self.output / ".refresh.lock").read_bytes(), first)
        self.assertFalse((self.output / ".refresh.lock").exists())

    def test_lock_released_on_job_error_and_changed_owner_is_not_deleted(self):
        with self.assertRaises(RuntimeError):
            with sync.run_lock(self.output):
                raise RuntimeError("job failed")
        self.assertFalse((self.output / ".refresh.lock").exists())
        replacement = '{"pid":555,"process_start":100}'
        with sync.run_lock(self.output):
            (self.output / ".refresh.lock").write_text(replacement, encoding="ascii")
        self.assertEqual((self.output / ".refresh.lock").read_text(), replacement)

    def test_process_identity_lookup_is_read_only_and_does_not_call_os_kill(self):
        with patch.object(sync.os, "kill", side_effect=AssertionError("os.kill is forbidden")):
            state = sync.process_state(os.getpid())
        self.assertEqual(state["status"], "alive")
        self.assertGreater(state["process_start"], 0)

    def test_insufficient_disk_stops_before_fetch_and_releases_owned_lock(self):
        with patch.object(sync.shutil, "disk_usage", return_value=argparse.Namespace(free=1)), \
             patch.object(sync, "fetch_store") as fetch, self.assertRaises(ValueError):
            sync.perform_refresh(self.args, self.config)
        fetch.assert_not_called()
        self.assertFalse((self.output / ".refresh.lock").exists())

    @unittest.skipUnless(os.name == "nt", "Windows API boundary check")
    def test_windows_process_permission_error_is_unknown_and_missing_pid_is_dead(self):
        import ctypes
        from unittest.mock import MagicMock
        kernel = MagicMock()
        kernel.OpenProcess.return_value = None
        with patch.object(ctypes, "WinDLL", return_value=kernel), patch.object(ctypes, "get_last_error", return_value=5):
            self.assertEqual(sync.process_state(555)["status"], "unknown")
        with patch.object(ctypes, "WinDLL", return_value=kernel), patch.object(ctypes, "get_last_error", return_value=87):
            self.assertEqual(sync.process_state(555)["status"], "dead")
        kernel.OpenProcess.assert_called_with(0x1000, False, 555)

    @unittest.skipUnless(shutil.which("git"), "Git required for private-config integration")
    def test_git_repo_must_ignore_private_config_and_refuse_tracked_credentials(self):
        repository = Path(self.temporary.name) / "git-safety"
        repository.mkdir()
        subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
        target = repository / "private" / "config.json"
        with self.assertRaises(ValueError):
            sync.private_config_path(target)
        ignore = repository / ".gitignore"
        ignore.write_text("private/\n", encoding="utf-8")
        original = ignore.read_bytes()
        self.assertEqual(sync.private_config_path(target), target.resolve())
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "-f", "--", "private/config.json"], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            sync.private_config_path(target)
        self.assertEqual(ignore.read_bytes(), original, "private checks never edit ignore rules")

    @unittest.skipUnless(shutil.which("git"), "Git required for private-config integration")
    def test_unsafe_git_init_is_rejected_before_reading_capture(self):
        repository = Path(self.temporary.name) / "public-git"
        repository.mkdir()
        subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
        target = repository / "private" / "config.json"
        args = argparse.Namespace(config=str(target), capture="unused", replace=False, erp_config="")
        with patch.object(sync, "read_capture") as capture, self.assertRaises(ValueError):
            sync.initialize(args)
        capture.assert_not_called()
        self.assertFalse(target.exists())

    def test_csv_product_text_cannot_start_a_formula(self):
        snapshot = dataset("STORE-A")
        snapshot["sales_windows"]["last_30d"]["rows"][0]["name"] = "=HYPERLINK(\"https://invalid.local\")"
        path = self.output / "safe.csv"
        sync.write_csv(path, [snapshot])
        import csv
        with path.open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertTrue(rows[0]["商品"].startswith("'="))


if __name__ == "__main__":
    unittest.main()
