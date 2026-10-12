"""Work-notice tests use fake HTTP only; they never contact DingTalk."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "coupang-ops" / "scripts" / "dingtalk_notify.py"
spec = importlib.util.spec_from_file_location("dingtalk_notify", SCRIPT)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, payload, timeout):
        self.calls.append((method, url, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def config():
    return {"app_key": "fake-app-key", "app_secret": "fake-app-secret", "agent_id": 123,
            "user_ids": ["staff-one"], "recipient_name": "倪笑缘"}


class DingTalkNotifyTests(unittest.TestCase):
    def _erp_database_fixture(self, directory):
        root = Path(directory)
        (root / "config").mkdir()
        (root / "config" / "config.py").write_text(
            "USER='fake-db-user'\nPASSWORD='fake%20db%20secret'\nDATABASE='fake-db'\n"
            "IPADDRESS='database.internal:3306'\nAPPKEY='fake-app-key'\nAPPSECRET='fake-app-secret'\nAGENT_ID=123\n",
            encoding="utf-8")
        return root

    def test_submit_is_accepted_and_exact_person_only(self):
        http = FakeHTTP({"accessToken": "fake-access-token", "expireIn": 7200}, {"errcode": 0, "task_id": 99})
        result = notify.send_work_notice(config(), "运营更新", "Cookie 状态：有效", http)
        self.assertEqual(result, {"status": "accepted", "task_id": 99})
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0][:3], ("POST", notify.TOKEN_URL, {"appKey": "fake-app-key", "appSecret": "fake-app-secret"}))
        _, url, body, _ = http.calls[1]
        self.assertEqual(urlsplit(url).hostname, "oapi.dingtalk.com")
        self.assertEqual(parse_qs(urlsplit(url).query), {"access_token": ["fake-access-token"]})
        self.assertEqual(body["userid_list"], "staff-one")
        self.assertIs(body["to_all_user"], False)
        self.assertNotIn("dept_id_list", body)
        self.assertNotIn("delivered", result.values())

    def test_missing_or_multiple_recipients_never_send(self):
        for candidate in ({}, {**config(), "user_ids": []}, {**config(), "user_ids": ["one", "two"]},
                          {**config(), "to_all_user": True}, {**config(), "recipient_name": ""}):
            http = FakeHTTP()
            self.assertEqual(notify.send_work_notice(candidate, "Title", "Text", http)["status"], "missing_config")
            self.assertEqual(http.calls, [])

    def test_errors_return_no_secret_response_or_token_url(self):
        for response in ({"errcode": 40014, "errmsg": "fake-app-secret access_token=fake-access-token"},
                         {"code": "fake-app-secret", "message": "fake-app-key"},
                         {"_transport_error": "fake-app-secret", "_http_status": 403}):
            http = FakeHTTP(response)
            result = notify.send_work_notice(config(), "Title", "Text", http)
            self.assertEqual(result["status"], "failed")
            serialized = json.dumps(result)
            for value in ("fake-app-secret", "fake-access-token", "fake-app-key", "access_token=", "https:"):
                self.assertNotIn(value, serialized)

    def test_send_failure_not_retried(self):
        http = FakeHTTP({"accessToken": "fake-token"}, {"errcode": 40035, "errmsg": "fake-token"})
        self.assertEqual(notify.send_work_notice(config(), "Title", "Text", http)["code"], 40035)
        self.assertEqual(len(http.calls), 2)

    def test_send_timeout_is_unknown_and_never_retried(self):
        http = FakeHTTP({"accessToken": "fake-token"}, TimeoutError("URL?access_token=fake-token fake-app-secret"))
        result = notify.send_work_notice(config(), "Title", "Text", http)
        self.assertEqual(result, {"status": "unknown", "stage": "send", "error": "transport_error"})
        self.assertEqual(len(http.calls), 2)

    def test_incomplete_send_response_does_not_claim_acceptance(self):
        for response in ({"task_id": 99}, {"errcode": 0}, {"errcode": False, "task_id": 99}):
            http = FakeHTTP({"accessToken": "fake-token"}, response)
            self.assertEqual(notify.send_work_notice(config(), "Title", "Text", http)["status"], "unknown")
            self.assertEqual(len(http.calls), 2)

    def test_secrets_and_cookie_header_content_blocked(self):
        for text in ("Cookie: session=private", "access_token=private", "fake-app-secret", "Authorization: Bearer private"):
            http = FakeHTTP()
            self.assertEqual(notify.send_work_notice(config(), "Title", text, http)["error"], "sensitive_content_blocked")
            self.assertEqual(http.calls, [])

    def test_delivery_requires_finished_progress_and_explicit_recipient(self):
        for result, status in (({"unread_user_id_list": ["staff-one"]}, "delivered"),
                               ({"unread_user_id_list": ["staff-one"], "read_user_id_list": None}, "delivered"),
                               ({"read_user_id_list": ["staff-one"]}, "delivered"),
                               ({"failed_user_id_list": ["staff-one"]}, "failed"),
                               ({"forbidden_list": [{"userid": "staff-one", "code": "143105"}]}, "failed"),
                               ({}, "unknown")):
            http = FakeHTTP({"accessToken": "fake-token"}, {"errcode": 0, "progress": {"status": 2}},
                            {"errcode": 0, "send_result": result})
            actual = notify.verify_delivery(config(), 99, http)
            self.assertEqual(actual["status"], status)
            self.assertEqual(actual["task_id"], 99)
            self.assertEqual(len(http.calls), 3)
            self.assertTrue(all(notify.SEND_URL not in call[1] for call in http.calls))

    def test_pending_task_is_not_delivery(self):
        http = FakeHTTP({"accessToken": "fake-token"}, {"errcode": 0, "progress": {"status": 1}})
        self.assertEqual(notify.verify_delivery(config(), 99, http), {"status": "pending", "task_id": 99})
        self.assertEqual(len(http.calls), 2)

    def test_load_json_requires_exact_name_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config(), ensure_ascii=False), encoding="utf-8")
            actual = notify.load_erp_config(path)
            self.assertEqual(actual, {"status": "configured", **config()})
            path.write_text(json.dumps({**config(), "recipient_name": "另一人"}), encoding="utf-8")
            actual = notify.load_erp_config(path)
            self.assertEqual(actual["status"], "missing_config")
            self.assertEqual(actual["missing"], ["user_ids"])
            self.assertNotIn("fake-app-secret", json.dumps(actual))

    def test_python_is_parsed_without_execution_and_named_map_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.py"
            path.write_text("raise RuntimeError('must never execute')\nAPPKEY='fake-app-key'\nAPPSECRET='fake-app-secret'\nAGENT_ID='123'\nRECIPIENTS={" + repr("倪笑缘") + ": 'staff-one'}\n", encoding="utf-8")
            self.assertEqual(notify.load_erp_config(path)["status"], "configured")

    def test_env_format_requires_named_single_recipient(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DINGTALK_APP_KEY=fake-app-key\nDINGTALK_APP_SECRET=fake-app-secret\nDINGTALK_AGENT_ID=123\n"
                            "DINGTALK_RECIPIENT_NAME=倪笑缘\nDINGTALK_USER_IDS=staff-one\n", encoding="utf-8")
            self.assertEqual(notify.load_erp_config(path), {"status": "configured", **config()})

    def test_erp_named_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.py").write_text("APPKEY='fake-app-key'\nAPPSECRET='fake-app-secret'\nAGENT_ID='123'", encoding="utf-8")
            (root / "notification.py").write_text("RECIPIENT_NAME=" + repr("倪笑缘") + "\ndef resolve_recipient_user_id():\n    override = os.environ.get('OZON_SALES_DINGTALK_USER_ID')\n", encoding="utf-8")
            with patch.dict(os.environ, {"OZON_SALES_DINGTALK_USER_ID": "staff-one"}):
                self.assertEqual(notify.load_erp_config(root)["user_ids"], ["staff-one"])
            with patch.dict(os.environ, {"OZON_SALES_DINGTALK_USER_ID": "staff-one,staff-two"}):
                self.assertEqual(notify.load_erp_config(root)["status"], "missing_config")

    def test_ambiguous_application_or_recipient_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in ([config(), {**config(), "app_key": "second-key"}],
                          [config(), {**config(), "user_ids": ["staff-two"]}],
                          {**config(), "user_ids": ["staff-one", "staff-two"]}):
                path.write_text(json.dumps(value), encoding="utf-8")
                actual = notify.load_erp_config(path)
                self.assertEqual(actual["status"], "missing_config")
                self.assertEqual(actual["reason"], "ambiguous_config")
                self.assertNotIn("app_secret", actual)

    def test_custom_endpoint_is_never_used(self):
        http = FakeHTTP({"accessToken": "fake-token"}, {"errcode": 0, "task_id": 99})
        notify.send_work_notice({**config(), "endpoint": "https://example.test/steal"}, "Title", "Text", http)
        self.assertTrue(all(urlsplit(call[1]).hostname in {"api.dingtalk.com", "oapi.dingtalk.com"} for call in http.calls))

    def test_verified_recipient_override_keeps_credentials_in_erp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._erp_database_fixture(directory)
            loaded = notify.load_erp_config(root, recipient_user_ids=["staff-one"])
            self.assertEqual(loaded, {"status": "configured", **config()})
            self.assertEqual(notify.load_erp_config(root, recipient_user_ids=["one", "two"])["status"], "missing_config")

    def test_database_resolution_is_bounded_read_only_and_exact(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = self._erp_database_fixture(directory)
            cursor = Mock()
            cursor.fetchall.return_value = [("staff-one",)]
            connection = Mock()
            connection.cursor.return_value = cursor
            connect = Mock(return_value=connection)
            fake = types.SimpleNamespace(connect=connect)
            with patch.dict("sys.modules", {"pymysql": fake}):
                actual = notify.resolve_erp_user_id(root)
            self.assertEqual(actual, {"status": "resolved", "recipient_name": "倪笑缘", "user_id": "staff-one"})
            kwargs = connect.call_args.kwargs
            self.assertEqual(kwargs["password"], "fake db secret")
            self.assertEqual(kwargs["connect_timeout"], 5)
            self.assertEqual(kwargs["read_timeout"], 10)
            query, parameters = cursor.execute.call_args.args
            self.assertTrue(query.startswith("SELECT user_uuid "))
            self.assertTrue(query.endswith("LIMIT 2"))
            self.assertEqual(parameters, ("倪笑缘", "倪笑缘"))
            self.assertEqual(cursor.execute.call_count, 1)
            cursor.close.assert_called_once()
            connection.close.assert_called_once()

    def test_database_ambiguous_and_error_results_are_sanitized(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = self._erp_database_fixture(directory)
            for rows, reason in (([], "erp_recipient_not_found"), ([("one",), ("two",)], "erp_recipient_ambiguous")):
                cursor = Mock()
                cursor.fetchall.return_value = rows
                connection = Mock()
                connection.cursor.return_value = cursor
                fake = types.SimpleNamespace(connect=Mock(return_value=connection))
                with patch.dict("sys.modules", {"pymysql": fake}):
                    actual = notify.resolve_erp_user_id(root)
                self.assertEqual(actual["status"], "missing_config")
                self.assertEqual(actual["reason"], reason)
            fake = types.SimpleNamespace(connect=Mock(side_effect=RuntimeError("fake-db-user fake db secret")))
            with patch.dict("sys.modules", {"pymysql": fake}):
                actual = notify.resolve_erp_user_id(root)
            self.assertEqual(actual["reason"], "erp_database_lookup_failed")
            self.assertNotIn("fake db secret", json.dumps(actual))

    def test_missing_database_dependency_does_not_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._erp_database_fixture(directory)
            with patch.dict("sys.modules", {"pymysql": None}):
                actual = notify.resolve_erp_user_id(root)
            self.assertEqual(actual["status"], "missing_config")
            self.assertEqual(actual["reason"], "pymysql_unavailable")


if __name__ == "__main__":
    unittest.main()
