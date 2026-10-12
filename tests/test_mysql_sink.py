"""Offline MySQL sink checks; no ERP or database is contacted."""

import copy
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "coupang-ops" / "scripts" / "mysql_sink.py"
spec = importlib.util.spec_from_file_location("mysql_sink", SCRIPT)
sink = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sink)
OBSERVED = "2026-10-12T12:00:00+09:00"


def dataset(store="STORE-A", skus=("SKU-1",), amount="123.450000000001"):
    windows, raw = {}, {}
    for name, start, end in (("last_30d", "2026-09-12", "2026-10-11"),
                             ("current_7d", "2026-10-05", "2026-10-11"),
                             ("previous_7d", "2026-09-28", "2026-10-04")):
        rows = [{"sku": sku, "name": "Item " + sku, "units": 2, "amount": amount,
                 "traffic": {"totalPageViews": 9, "pvToOrder": "0.1234567890123456789"}} for sku in skus]
        windows[name] = {"start_date": start, "end_date": end, "complete": True,
                         "captured_at": "2026-10-12T11:59:00+09:00", "rows": rows}
        raw[name] = [{"vendorItemDetails": {"vendorId": store, "vendorItemId": row["sku"], "itemName": row["name"],
                                            "productName": row["name"], "registrationType": "NORMAL", "isOOS": False, "inventoryId": None},
                      "businessInsightsMetricsResponse": {"totalUnitsSold": 2, "totalGmv": amount, **row["traffic"]}}
                     for row in rows]
    return {"store": store, "currency": "KRW", "timezone": "Asia/Seoul", "analysis_date": "2026-10-12",
            "observed_at": OBSERVED, "sales_basis": "platform_reported", "refresh_status": "success", "sales_windows": windows, "raw_windows": raw}


def states(*stores):
    return [{"store": store, "status": "success", "fetch_status": "success"} for store in stores]


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []
        self.last = None
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.last = sql
        db = self.connection
        if sql == sink.CREATE_SQL:
            db.exists = True
            return 0
        if sql == sink.UPSERT_SQL:
            row = dict(zip(sink.COLUMNS, params[:len(sink.COLUMNS)]))
            if row["store"] == db.fail_store and row["row_type"] == "status":
                raise RuntimeError("FAKE_DB_PASSWORD raw SQL business values must not escape")
            key = (row["store"], row["window_key"], row["sku"])
            if row["collected_at"] is None and key in db.rows:
                row["collected_at"] = db.rows[key]["collected_at"]
            db.rows[key] = row
        elif sql == sink.RETIRE_SQL:
            attempt, store, window = params
            for row in db.rows.values():
                if row["store"] == store and row["window_key"] == window and row["row_type"] == "sku":
                    row.update(is_current=False, last_attempt_at=attempt)
        elif sql == sink.STALE_SQL:
            attempt, store = params
            for row in db.rows.values():
                if row["store"] == store and row["row_type"] == "sku":
                    row.update(refresh_status="stale", last_attempt_at=attempt)
        return 1

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def fetchone(self):
        return ("InnoDB", self.connection.owner) if self.connection.exists else None

    def fetchall(self):
        if self.last == sink.COLUMN_META_SQL:
            rows = [(name, *specification) for name, specification in sink.SCHEMA.items()]
            if not self.connection.compatible:
                rows = [row for row in rows if row[0] != "amount"]
            return rows
        if self.last == sink.PK_META_SQL:
            return [("store",), ("window_key",), ("sku",)]
        if self.last == sink.EXTRA_UNIQUE_SQL:
            return [("unique_sku",)] if self.connection.extra_unique else []
        if self.last == sink.TRIGGER_META_SQL:
            return [("write_other_erp_table",)] if self.connection.triggers else []
        raise AssertionError("Unexpected metadata query")

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, *, exists=False, owner=sink.OWNERSHIP, compatible=True, fail_store=None,
                 extra_unique=False, triggers=False, fail_commit_number=None):
        self.exists, self.owner, self.compatible, self.fail_store = exists, owner, compatible, fail_store
        self.extra_unique, self.triggers = extra_unique, triggers
        self.fail_commit_number = fail_commit_number
        self.rows = {}
        self.before = None
        self.commits = self.begins = self.rollbacks = 0
        self.closed = False
        self.cursor_value = FakeCursor(self)

    def cursor(self):
        return self.cursor_value

    def begin(self):
        self.begins += 1
        self.before = copy.deepcopy(self.rows)

    def commit(self):
        self.commits += 1
        self.before = None
        if self.commits == self.fail_commit_number:
            # Model a successful server commit whose acknowledgement was lost.
            raise RuntimeError("FAKE_DB_PASSWORD lost commit acknowledgement")

    def rollback(self):
        self.rollbacks += 1
        if self.before is not None:
            self.rows = self.before
        self.before = None

    def close(self):
        self.closed = True


class MySQLSinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "config.py").write_text(
            "raise RuntimeError('ERP must never execute')\nUSER='FAKE_DB_USER'\nPASSWORD='FAKE%20DB%20PASSWORD'\n"
            "DATABASE='fake_db'\nIPADDRESS='fake.database:3306'\n", encoding="utf-8")
        self.connect_calls = []

    def persist(self, data, state, connection=None, observed=OBSERVED):
        connection = connection or FakeConnection()
        def connect(**kwargs):
            self.connect_calls.append(kwargs)
            return connection
        with patch.dict("sys.modules", {"pymysql": types.SimpleNamespace(connect=connect)}):
            result = sink.persist_refresh(self.root, data, state, observed)
        return result, connection

    def test_create_upsert_decimal_utc_and_bound_values(self):
        data = dataset(skus=("SKU-1", "SKU'); DROP TABLE other;--"))
        result, db = self.persist([data], states("STORE-A"))
        self.assertEqual(result, {"status": "updated", "table": sink.TABLE, "rows_written": 7, "stores": 1})
        self.assertEqual(self.connect_calls[0]["password"], "FAKE DB PASSWORD")
        self.assertEqual(db.begins, 1)
        self.assertEqual(db.commits, 2)  # new-table DDL followed by the store commit
        for sql, values in db.cursor_value.calls:
            self.assertNotIn("DROP TABLE other", sql)
            self.assertNotIn("FAKE DB PASSWORD", sql)
            if sql == sink.UPSERT_SQL:
                self.assertIn("ON DUPLICATE KEY UPDATE", sql)
                self.assertEqual(sql.count("%s"), len(values))
        row = db.rows[("STORE-A", "current_7d", "SKU-1")]
        self.assertIsInstance(row["amount"], Decimal)
        self.assertEqual(row["amount"], Decimal("123.450000000001"))
        self.assertEqual(row["collected_at"], datetime(2026, 10, 12, 2, 59))
        self.assertEqual(row["last_attempt_at"], datetime(2026, 10, 12, 3))
        self.assertEqual(json.loads(row["response_json"]), data["raw_windows"]["current_7d"][0])
        self.assertTrue(db.closed and db.cursor_value.closed)

    def test_real_pymysql_batch_formats_bound_values_without_postfix_arguments(self):
        try:
            import pymysql
        except ImportError:
            self.skipTest("Optional ERP driver is unavailable")
        connection = pymysql.connections.Connection(defer_connect=True, charset="utf8mb4")
        connection.server_status = 0
        cursor = connection.cursor()
        _, prepared = sink._prepare([dataset(skus=("SKU-1", "SKU-2"))], states("STORE-A"), OBSERVED)
        sent = []
        def execute(query, args=None):
            sent.append(query)
            return 2
        with patch.object(cursor, "execute", side_effect=execute):
            cursor.executemany(sink.UPSERT_SQL, [sink._bound_row(row) for row in prepared[0][2]])
        self.assertEqual(len(sent), 1)
        self.assertNotIn(b"%s", bytes(sent[0]))
        self.assertIn(b"VALUES(`amount`)", bytes(sent[0]))

    def test_four_status_rows_are_unique_and_store_isolated(self):
        names = ("STORE-A", "STORE-B", "STORE-C", "STORE-D")
        result, db = self.persist([dataset(name) for name in names], states(*names))
        self.assertEqual(result["rows_written"], 16)
        self.assertEqual(result["stores"], 4)
        self.assertEqual(db.begins, 4)
        self.assertEqual(sum(row["row_type"] == "status" for row in db.rows.values()), 4)
        self.assertEqual(len(db.rows), 16)

    def test_wing_basis_and_observation_are_validated_before_connect(self):
        for field, value in (("timezone", "Asia/Shanghai"), ("currency", "USD"),
                             ("sales_basis", "net_confirmed"), ("observed_at", "not-a-date"),
                             ("observed_at", "2026-10-12T11:58:00+09:00")):
            with self.subTest(field=field, value=value):
                data = dataset()
                data[field] = value
                result, db = self.persist([data], states("STORE-A"))
                self.assertEqual(result["reason"], "validation_failed")
                self.assertEqual(db.cursor_value.calls, [])
        self.assertEqual(self.connect_calls, [])

    def test_success_status_collection_time_uses_final_observation_and_attempt_stays_start(self):
        result, db = self.persist([dataset()], states("STORE-A"), observed="2026-10-12T11:00:00+09:00")
        self.assertEqual(result["status"], "updated")
        status = db.rows[("STORE-A", "__status__", "")]
        self.assertEqual(status["collected_at"], datetime(2026, 10, 12, 3))
        self.assertEqual(status["last_attempt_at"], datetime(2026, 10, 12, 2))

    def test_failure_preserves_metrics_and_collected_at(self):
        _, db = self.persist([dataset()], states("STORE-A"))
        original = copy.deepcopy(db.rows)
        cached = dataset(amount="999")
        cached["refresh_status"] = "stale"
        state = [{"store": "STORE-A", "status": "success", "fetch_status": "auth_required", "message": "Cookie: FAKE_SECRET"}]
        result, db = self.persist([cached], state, db, "2026-10-12T12:30:00+09:00")
        self.assertEqual(result["rows_written"], 1)
        for key, row in db.rows.items():
            self.assertEqual(row["collected_at"], original[key]["collected_at"])
            self.assertNotIn("FAKE_SECRET", row["response_json"])
            if row["row_type"] == "sku":
                self.assertEqual(row["amount"], original[key]["amount"])
                self.assertEqual(row["units"], original[key]["units"])
                self.assertEqual(row["refresh_status"], "stale")

    def test_stale_cache_cannot_be_labeled_as_current_success(self):
        data = dataset()
        data["refresh_status"] = "stale"
        result, db = self.persist([data], states("STORE-A"))
        self.assertEqual(result["reason"], "validation_failed")
        self.assertEqual(db.cursor_value.calls, [])
        # Cached business rows are deliberately ignored on a failed collection.
        data["raw_windows"] = {"arbitrary_cache": "must not be upserted"}
        result, db = self.persist([data], [{"store": "STORE-A", "fetch_status": "fetch_failed"}])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["rows_written"], 1)
        self.assertEqual(len(db.rows), 1)
        self.assertEqual(next(iter(db.rows.values()))["row_type"], "status")

    def test_unknown_sales_metrics_remain_null(self):
        data = dataset(amount=None)
        for window in sink.WINDOWS:
            data["sales_windows"][window]["rows"][0]["units"] = None
            data["raw_windows"][window][0]["businessInsightsMetricsResponse"]["totalUnitsSold"] = None
        result, db = self.persist([data], states("STORE-A"))
        self.assertEqual(result["status"], "updated")
        row = db.rows[("STORE-A", "current_7d", "SKU-1")]
        self.assertIsNone(row["units"])
        self.assertIsNone(row["amount"])

    def test_complete_refresh_retires_absent_skus_without_deleting(self):
        _, db = self.persist([dataset(skus=("SKU-1", "SKU-2"))], states("STORE-A"))
        old = copy.deepcopy(db.rows[("STORE-A", "current_7d", "SKU-2")])
        self.persist([dataset()], states("STORE-A"), db)
        row = db.rows[("STORE-A", "current_7d", "SKU-2")]
        self.assertFalse(row["is_current"])
        self.assertEqual(row["amount"], old["amount"])
        self.assertEqual(row["collected_at"], old["collected_at"])
        self.assertEqual(len(db.rows), 7)
        self.assertTrue(db.rows[("STORE-A", "current_7d", "SKU-1")]["is_current"])

    def test_mixed_store_currency_dates_and_raw_order_rejected_before_connect(self):
        malformed = []
        for field, value in (("store", "STORE-B"), ("currency", "USD"), ("start_date", "2020-01-01")):
            data = dataset()
            data["sales_windows"]["current_7d"]["rows"][0][field] = value
            malformed.append(data)
        data = dataset(skus=("SKU-1", "SKU-2"))
        data["raw_windows"]["current_7d"].reverse()
        malformed.append(data)
        data = dataset()
        data["sales_windows"]["current_7d"]["complete"] = False
        malformed.append(data)
        data = dataset()
        data["raw_windows"]["current_7d"][0]["vendorItemDetails"]["vendorId"] = "STORE-B"
        malformed.append(data)
        for data in malformed:
            result, db = self.persist([data], states("STORE-A"))
            self.assertEqual(result["reason"], "validation_failed")
            self.assertEqual(db.cursor_value.calls, [])
        self.assertEqual(self.connect_calls, [])

    def test_amount_precision_and_scale_never_round_or_overflow(self):
        for amount in ("100000000000000000000000000", "-100000000000000000000000000", "0.0000000000001", "NaN", "Infinity", 1.1):
            result, _ = self.persist([dataset(amount=amount)], states("STORE-A"))
            self.assertEqual(result["reason"], "validation_failed")
        result, db = self.persist([dataset(amount=str(sink.MAX_AMOUNT))], states("STORE-A"))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(db.rows[("STORE-A", "current_7d", "SKU-1")]["amount"], sink.MAX_AMOUNT)
        result, _ = self.persist([dataset(amount="1.0000000000000000")], states("STORE-A"))
        self.assertEqual(result["status"], "updated")

    def test_signed_backend_sales_and_orders_persist_without_changing_traffic_counts(self):
        data = dataset(amount="-16990.0")
        for key in sink.WINDOWS:
            data["sales_windows"][key]["rows"][0]["units"] = -1
            data["sales_windows"][key]["rows"][0]["traffic"]["totalOrders"] = -1
            data["raw_windows"][key][0]["businessInsightsMetricsResponse"].update(totalUnitsSold=-1, totalOrders=-1)
        result, db = self.persist([data], states("STORE-A"))
        self.assertEqual(result["status"], "updated")
        saved = db.rows[("STORE-A", "last_30d", "SKU-1")]
        self.assertEqual((saved["units"], saved["amount"]), (-1, Decimal("-16990.0")))
        self.assertEqual(json.loads(saved["traffic_json"])["totalOrders"], -1)
        data["sales_windows"]["last_30d"]["rows"][0]["traffic"]["totalPageViews"] = -1
        data["raw_windows"]["last_30d"][0]["businessInsightsMetricsResponse"]["totalPageViews"] = -1
        result, _ = self.persist([data], states("STORE-A"))
        self.assertEqual(result["reason"], "validation_failed")

    def test_only_business_allowlist_is_persisted(self):
        data = dataset()
        for name in sink.WINDOWS:
            raw = data["raw_windows"][name][0]
            raw["headers"] = {"Cookie": "FAKE_CAPTURE_SECRET"}
            raw["vendorItemDetails"]["Cookie"] = "FAKE_CAPTURE_SECRET"
            raw["businessInsightsMetricsResponse"]["access_token"] = "FAKE_CAPTURE_SECRET"
            data["sales_windows"][name]["rows"][0]["traffic"]["Cookie"] = "FAKE_CAPTURE_SECRET"
        result, db = self.persist([data], states("STORE-A"))
        self.assertEqual(result["status"], "updated")
        self.assertNotIn("FAKE_CAPTURE_SECRET", json.dumps(list(db.rows.values()), default=str))

    def test_failed_store_rollback_retains_previous_store_commits(self):
        db = FakeConnection(fail_store="STORE-B")
        result, db = self.persist([dataset("STORE-A"), dataset("STORE-B")], states("STORE-A", "STORE-B"), db)
        self.assertEqual(result, {"status": "failed", "table": sink.TABLE, "reason": "database_write_failed", "rows_written": 4, "stores": 1})
        self.assertEqual(db.rollbacks, 1)
        self.assertTrue(all(row["store"] == "STORE-A" for row in db.rows.values()))
        self.assertNotIn("FAKE_DB_PASSWORD", json.dumps(result))

    def test_existing_unowned_or_incompatible_table_is_never_mutated(self):
        for db in (FakeConnection(exists=True, owner="someone else"), FakeConnection(exists=True, compatible=False)):
            result, db = self.persist([dataset()], states("STORE-A"), db)
            self.assertEqual(result["reason"], "table_schema_or_ownership_mismatch")
            self.assertEqual(db.begins, 0)
            self.assertEqual(db.rows, {})
            self.assertTrue(all(not sql.startswith(("CREATE", "INSERT", "UPDATE", "ALTER")) for sql, _ in db.cursor_value.calls))

    def test_extra_unique_index_and_triggers_block_all_writes(self):
        for db in (FakeConnection(exists=True, extra_unique=True), FakeConnection(exists=True, triggers=True)):
            result, db = self.persist([dataset()], states("STORE-A"), db)
            self.assertEqual(result["reason"], "table_schema_or_ownership_mismatch")
            self.assertEqual(db.begins, 0)
            self.assertEqual(db.rows, {})
            self.assertTrue(all(not sql.startswith(("CREATE", "INSERT", "UPDATE", "ALTER")) for sql, _ in db.cursor_value.calls))

    def test_commit_acknowledgement_loss_is_unknown_and_not_counted_or_retried(self):
        db = FakeConnection(exists=True, fail_commit_number=2)
        result, db = self.persist([dataset("STORE-A"), dataset("STORE-B")], states("STORE-A", "STORE-B"), db)
        self.assertEqual(result, {"status": "unknown", "table": sink.TABLE, "reason": "commit_outcome_unknown", "rows_written": 4, "stores": 1})
        self.assertEqual(db.begins, 2)
        self.assertEqual(db.commits, 2)
        self.assertEqual(db.rollbacks, 0)
        # The second store may actually be present; a rollback cannot prove loss.
        self.assertEqual(len(db.rows), 8)
        self.assertEqual(sum(row["store"] == "STORE-B" for row in db.rows.values()), 4)
        self.assertTrue(db.closed)
        self.assertNotIn("FAKE_DB_PASSWORD", json.dumps(result))

    def test_new_table_commit_acknowledgement_loss_is_unknown(self):
        db = FakeConnection(fail_commit_number=1)
        result, db = self.persist([dataset()], states("STORE-A"), db)
        self.assertEqual(result, {"status": "unknown", "table": sink.TABLE, "reason": "commit_outcome_unknown", "rows_written": 0, "stores": 0})
        self.assertEqual(db.begins, 0)
        self.assertTrue(db.exists)
        self.assertTrue(db.closed)

    def test_missing_dependency_and_duplicate_store_are_safe(self):
        with patch.dict("sys.modules", {"pymysql": None}):
            result = sink.persist_refresh(self.root, [], [{"store": "STORE-A", "fetch_status": "auth_required"}], OBSERVED)
        self.assertEqual(result["reason"], "pymysql_unavailable")
        result, _ = self.persist([dataset()], states("STORE-A", "STORE-A"))
        self.assertEqual(result["reason"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
