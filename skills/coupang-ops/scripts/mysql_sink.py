"""Persist authorized WING snapshots in one new ERP MySQL table.

ERP configuration is parsed as data. No ERP modules are imported or executed,
no credentials are copied to files, and database errors never expose SQL values.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from urllib.parse import unquote


TABLE = "coupang_ops_sales_snapshot"
WINDOWS = ("last_30d", "current_7d", "previous_7d")
DETAIL_FIELDS = ("vendorId", "vendorItemId", "itemName", "productName", "registrationType", "isOOS", "inventoryId")
COUNT_TRAFFIC = ("totalOrders", "totalUniqueVisitor", "totalPageViews", "srpClick", "searchVolume")
METRIC_FIELDS = ("totalUnitsSold", "totalGmv", *COUNT_TRAFFIC, "pvToOrder")
COLUMNS = ("store", "window_key", "sku", "row_type", "start_date", "end_date", "product_name", "units", "amount",
           "currency", "traffic_json", "response_json", "collected_at", "last_attempt_at", "refresh_status", "is_current", "window_complete")
MAX_AMOUNT = Decimal("99999999999999999999999999.999999999999")
MAX_BIGINT = 9223372036854775807
OWNERSHIP = "coupang-ops:authorized-sales-snapshot:v1"
CREATE_SQL = """CREATE TABLE IF NOT EXISTS `coupang_ops_sales_snapshot` (
  `store` VARCHAR(64) NOT NULL,
  `window_key` VARCHAR(24) NOT NULL,
  `sku` VARCHAR(128) NOT NULL,
  `row_type` VARCHAR(8) NOT NULL,
  `start_date` DATE NULL,
  `end_date` DATE NULL,
  `product_name` TEXT NULL,
  `units` BIGINT NULL,
  `amount` DECIMAL(38,12) NULL,
  `currency` CHAR(3) NULL,
  `traffic_json` LONGTEXT NULL,
  `response_json` LONGTEXT NULL,
  `collected_at` DATETIME NULL,
  `last_attempt_at` DATETIME NOT NULL,
  `refresh_status` VARCHAR(24) NOT NULL,
  `is_current` BOOL NOT NULL,
  `window_complete` BOOL NOT NULL,
  PRIMARY KEY (`store`, `window_key`, `sku`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
COMMENT='coupang-ops:authorized-sales-snapshot:v1'"""
# All SQL identifiers come from this module's fixed schema. Every value,
# including duplicate-key updates, is bound through the driver parameters.
UPSERT_SQL = ("INSERT INTO `coupang_ops_sales_snapshot` (" + ",".join("`" + field + "`" for field in COLUMNS)
              + ") VALUES (" + ",".join(["%s"] * len(COLUMNS)) + ") ON DUPLICATE KEY UPDATE "
              + ",".join("`" + field + "`=" + ("COALESCE(%s,`collected_at`)" if field == "collected_at" else "%s")
                         for field in COLUMNS[3:]))
RETIRE_SQL = ("UPDATE `coupang_ops_sales_snapshot` SET `is_current`=0,`last_attempt_at`=%s "
              "WHERE `store`=%s AND `window_key`=%s AND `row_type`='sku'")
STALE_SQL = ("UPDATE `coupang_ops_sales_snapshot` SET `refresh_status`='stale',`last_attempt_at`=%s "
             "WHERE `store`=%s AND `row_type`='sku'")
TABLE_META_SQL = ("SELECT ENGINE,TABLE_COMMENT FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s")
COLUMN_META_SQL = ("SELECT COLUMN_NAME,DATA_TYPE,CHARACTER_MAXIMUM_LENGTH,NUMERIC_PRECISION,NUMERIC_SCALE,IS_NULLABLE,COLLATION_NAME "
                   "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION")
PK_META_SQL = ("SELECT COLUMN_NAME FROM information_schema.STATISTICS "
               "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX")
EXTRA_UNIQUE_SQL = ("SELECT INDEX_NAME FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND NON_UNIQUE=0 AND INDEX_NAME<>'PRIMARY'")
TRIGGER_META_SQL = ("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=%s")
SCHEMA = {
    "store": ("varchar", 64, None, None, "NO", "utf8mb4_bin"),
    "window_key": ("varchar", 24, None, None, "NO", "utf8mb4_bin"),
    "sku": ("varchar", 128, None, None, "NO", "utf8mb4_bin"),
    "row_type": ("varchar", 8, None, None, "NO", "utf8mb4_bin"),
    "start_date": ("date", None, None, None, "YES", None),
    "end_date": ("date", None, None, None, "YES", None),
    "product_name": ("text", None, None, None, "YES", "utf8mb4_bin"),
    "units": ("bigint", None, 19, 0, "YES", None),
    "amount": ("decimal", None, 38, 12, "YES", None),
    "currency": ("char", 3, None, None, "YES", "utf8mb4_bin"),
    "traffic_json": ("longtext", None, None, None, "YES", "utf8mb4_bin"),
    "response_json": ("longtext", None, None, None, "YES", "utf8mb4_bin"),
    "collected_at": ("datetime", None, None, None, "YES", None),
    "last_attempt_at": ("datetime", None, None, None, "NO", None),
    "refresh_status": ("varchar", 24, None, None, "NO", "utf8mb4_bin"),
    "is_current": ("tinyint", None, 3, 0, "NO", None),
    "window_complete": ("tinyint", None, 3, 0, "NO", None),
}


def _failure(reason, rows=0, stores=0):
    return {"status": "failed", "table": TABLE, "reason": reason, "rows_written": rows, "stores": stores}


def _commit_unknown(rows=0, stores=0):
    return {"status": "unknown", "table": TABLE, "reason": "commit_outcome_unknown", "rows_written": rows, "stores": stores}


def _database_config(erp_path):
    root = Path(erp_path)
    path = root / "config" / "config.py" if root.is_dir() else root
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    values = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"USER", "PASSWORD", "DATABASE", "IPADDRESS"}:
                value = ast.literal_eval(node.value)
                if target.id in values and values[target.id] != value:
                    raise ValueError("ambiguous_database_config")
                values[target.id] = value
    if not all(isinstance(values.get(field), str) and values[field].strip() for field in ("USER", "PASSWORD", "DATABASE", "IPADDRESS")):
        raise ValueError("database_config_missing")
    host, port_text = values["IPADDRESS"].strip().rsplit(":", 1)
    port = int(port_text)
    if not host or not 1 <= port <= 65535:
        raise ValueError("database_address_invalid")
    return {"host": host, "port": port, "user": values["USER"], "password": unquote(values["PASSWORD"]), "database": values["DATABASE"]}


def _identifier(value, length):
    if type(value) is int and value >= 0:
        value = str(value)
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > length
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("identifier_invalid")
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date_invalid")
    result = date.fromisoformat(value)
    if result.year < 1000:
        raise ValueError("date_out_of_range")
    return result


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("timestamp_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    result = parsed.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    if result.year < 1000:
        raise ValueError("timestamp_out_of_range")
    return result


def _decimal(value, *, monetary=True, signed=False):
    if value is None:
        return None
    if type(value) not in (int, str, Decimal) or len(str(value)) > 128:
        raise ValueError("decimal_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("decimal_invalid") from None
    if not number.is_finite() or (number < 0 and not signed) or abs(number.adjusted()) > 100:
        raise ValueError("decimal_out_of_range")
    if monetary:
        if number > MAX_AMOUNT or number < MAX_AMOUNT.copy_negate():
            raise ValueError("amount_out_of_range")
        digits = list(number.as_tuple().digits)
        exponent = number.as_tuple().exponent
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if digits and exponent < -12:
            raise ValueError("amount_scale_out_of_range")
    return number


def _count(value, *, signed=False):
    if value is None:
        return None
    if type(value) is int:
        result = value
    else:
        number = _decimal(value, monetary=False, signed=signed)
        if number is None or number != number.to_integral_value():
            raise ValueError("count_invalid")
        result = int(number)
    if not (-MAX_BIGINT if signed else 0) <= result <= MAX_BIGINT:
        raise ValueError("count_out_of_range")
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                      default=lambda item: str(item) if isinstance(item, Decimal) else (_ for _ in ()).throw(TypeError()))


def _context(record, store, currency, start=None, end=None):
    if not isinstance(record, dict):
        raise ValueError("record_invalid")
    for field, expected in (("store", store), ("currency", currency), ("start_date", start), ("end_date", end),
                            ("startDate", start), ("endDate", end)):
        if field in record and expected is not None and record[field] != expected:
            raise ValueError("record_context_mismatch")


def _prepare_dataset(dataset, store, attempt):
    _context(dataset, store, dataset.get("currency"))
    if dataset.get("refresh_status") not in (None, "success") or dataset.get("data_kind") == "synthetic_demo":
        raise ValueError("dataset_not_fresh")
    if (dataset.get("timezone") != "Asia/Seoul" or dataset.get("currency") != "KRW"
            or dataset.get("sales_basis") != "platform_reported"):
        raise ValueError("wing_basis_mismatch")
    observed = _timestamp(dataset.get("observed_at"))
    currency = dataset.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency_invalid")
    day = _date(dataset.get("analysis_date"))
    bounds = {
        "last_30d": (day - timedelta(days=30), day - timedelta(days=1)),
        "current_7d": (day - timedelta(days=7), day - timedelta(days=1)),
        "previous_7d": (day - timedelta(days=14), day - timedelta(days=8)),
    }
    windows, raw_windows = dataset.get("sales_windows"), dataset.get("raw_windows")
    if not isinstance(windows, dict) or set(windows) != set(WINDOWS) or not isinstance(raw_windows, dict) or set(raw_windows) != set(WINDOWS):
        raise ValueError("three_windows_required")
    prepared = []
    for window_key in WINDOWS:
        window = windows[window_key]
        if not isinstance(window, dict) or window.get("complete") is not True:
            raise ValueError("window_incomplete")
        start, end = _date(window.get("start_date")), _date(window.get("end_date"))
        if (start, end) != bounds[window_key]:
            raise ValueError("window_dates_mismatch")
        _context(window, store, currency, str(start), str(end))
        rows, raw_rows = window.get("rows"), raw_windows[window_key]
        if not isinstance(rows, list) or not isinstance(raw_rows, list) or len(rows) != len(raw_rows):
            raise ValueError("raw_order_mismatch")
        collected = _timestamp(window.get("captured_at", dataset.get("observed_at")))
        if collected > observed:
            raise ValueError("capture_after_observation")
        seen = set()
        for row, raw in zip(rows, raw_rows):
            _context(row, store, currency, str(start), str(end))
            _context(raw, store, currency, str(start), str(end))
            sku = _identifier(row.get("sku"), 128)
            if sku in seen:
                raise ValueError("duplicate_sku")
            seen.add(sku)
            name = row.get("name")
            if not isinstance(name, str) or not name.strip() or len(name.encode("utf-8")) > 65535:
                raise ValueError("product_name_invalid")
            if "units" not in row or "amount" not in row:
                raise ValueError("metrics_required")
            units, amount = _count(row["units"], signed=True), _decimal(row["amount"], signed=True)
            details, metrics = raw.get("vendorItemDetails"), raw.get("businessInsightsMetricsResponse")
            if not isinstance(details, dict) or not isinstance(metrics, dict):
                raise ValueError("business_response_required")
            if (_identifier(details.get("vendorId"), 64) != store or _identifier(details.get("vendorItemId"), 128) != sku
                    or (details.get("itemName") or details.get("productName")) != name):
                raise ValueError("raw_order_mismatch")
            _context(details, store, currency, str(start), str(end))
            _context(metrics, store, currency, str(start), str(end))
            if "totalUnitsSold" not in metrics or "totalGmv" not in metrics or _count(metrics["totalUnitsSold"], signed=True) != units or _decimal(metrics["totalGmv"], signed=True) != amount:
                raise ValueError("raw_metrics_mismatch")
            traffic = row.get("traffic", {})
            if not isinstance(traffic, dict):
                raise ValueError("traffic_invalid")
            safe_traffic = {}
            for field in (*COUNT_TRAFFIC, "pvToOrder"):
                if field in traffic:
                    value = _decimal(traffic[field], monetary=False, signed=True) if field == "pvToOrder" else _count(traffic[field], signed=field == "totalOrders")
                    if field in metrics:
                        raw_value = _decimal(metrics[field], monetary=False, signed=True) if field == "pvToOrder" else _count(metrics[field], signed=field == "totalOrders")
                        if value != raw_value:
                            raise ValueError("raw_traffic_mismatch")
                    safe_traffic[field] = traffic[field]
            safe_details = {key: details[key] for key in DETAIL_FIELDS if key in details}
            if any(type(value) not in (str, int, bool, type(None)) for value in safe_details.values()):
                raise ValueError("business_details_invalid")
            safe_metrics = {}
            for field in METRIC_FIELDS:
                if field in metrics:
                    if field in {"totalGmv", "pvToOrder"}:
                        _decimal(metrics[field], monetary=field == "totalGmv", signed=True)
                    else:
                        _count(metrics[field], signed=field in {"totalUnitsSold", "totalOrders"})
                    safe_metrics[field] = metrics[field]
            business = {"vendorItemDetails": safe_details, "businessInsightsMetricsResponse": safe_metrics}
            prepared.append((store, window_key, sku, "sku", start, end, name, units, amount, currency,
                             _json(safe_traffic), _json(business), collected, attempt, "success", True, True))
    return prepared, currency, observed


def _prepare(datasets, store_states, observed_at):
    attempt = _timestamp(observed_at)
    if not isinstance(datasets, list) or not isinstance(store_states, list) or not 1 <= len(store_states) <= 4:
        raise ValueError("refresh_arguments_invalid")
    states = {}
    for state in store_states:
        if not isinstance(state, dict):
            raise ValueError("store_state_invalid")
        store = _identifier(state.get("store"), 64)
        if store in states:
            raise ValueError("duplicate_store_state")
        states[store] = state
    by_store = {}
    for dataset in datasets:
        if not isinstance(dataset, dict):
            raise ValueError("dataset_invalid")
        store = _identifier(dataset.get("store"), 64)
        if store not in states or store in by_store:
            raise ValueError("dataset_store_mismatch")
        by_store[store] = dataset
    prepared = []
    for store, state in states.items():
        fetch_status = state.get("fetch_status", state.get("status"))
        if fetch_status not in {"success", "auth_required", "fetch_failed", "failed", "analysis_failed"}:
            raise ValueError("fetch_status_invalid")
        rows, currency, collected = [], None, None
        if fetch_status == "success":
            if store not in by_store:
                raise ValueError("fresh_dataset_required")
            rows, currency, collected = _prepare_dataset(by_store[store], store, attempt)
        status_row = (store, "__status__", "", "status", None, None, None, None, None, currency, "{}",
                      _json({"fetch_status": fetch_status}), collected,
                      attempt, fetch_status, True, fetch_status == "success")
        prepared.append((store, fetch_status, rows, status_row))
    return attempt, prepared


def _bound_row(row):
    return row + row[3:]


def _schema_owned(cursor):
    cursor.execute(TABLE_META_SQL, (TABLE,))
    table = cursor.fetchone()
    if not isinstance(table, (list, tuple)) or len(table) != 2 or table[0].lower() != "innodb" or table[1] != OWNERSHIP:
        return False
    cursor.execute(COLUMN_META_SQL, (TABLE,))
    rows = cursor.fetchall()
    actual = {}
    for row in rows:
        if isinstance(row, (tuple, list)) and len(row) == 7:
            specification = list(row[1:])
            if specification[0] in {"text", "longtext"}:
                specification[1] = None  # byte limits are implicit in these SQL types
            actual[row[0]] = tuple(specification)
    if len(rows) != len(SCHEMA) or actual != SCHEMA:
        return False
    cursor.execute(PK_META_SQL, (TABLE,))
    primary = cursor.fetchall()
    if primary != (("store",), ("window_key",), ("sku",)) and primary != [("store",), ("window_key",), ("sku",)]:
        return False
    # An extra UNIQUE key could make ON DUPLICATE KEY UPDATE match a row from
    # another store. Triggers could mutate other ERP tables. Reject both.
    cursor.execute(EXTRA_UNIQUE_SQL, (TABLE,))
    if cursor.fetchall() not in ([], ()):
        return False
    cursor.execute(TRIGGER_META_SQL, (TABLE,))
    return cursor.fetchall() in ([], ())


def persist_refresh(erp_path, datasets: list, store_states: list, observed_at: str) -> dict:
    """Validate the whole refresh, then commit one transaction per store.

    rows_written counts submitted SKU rows plus one status row per committed
    store, not MySQL's changed-row count. A later-store failure retains earlier
    committed stores and reports their counts. Write failures attempt rollback.
    A commit exception has an unknown outcome: its store is excluded from the
    confirmed counts and must not be automatically retried as a failed write.
    Cached stale datasets never overwrite SKU metrics or collection timestamps.
    """
    try:
        attempt, prepared = _prepare(datasets, store_states, observed_at)
    except Exception:
        return _failure("validation_failed")
    try:
        config = _database_config(erp_path)
    except Exception:
        return _failure("erp_database_config_unavailable")
    try:
        import pymysql
    except ImportError:
        return _failure("pymysql_unavailable")
    connection = None
    cursor = None
    written = committed = 0
    try:
        connection = pymysql.connect(**config, charset="utf8mb4", connect_timeout=5, read_timeout=15,
                                     write_timeout=15, autocommit=False)
        cursor = connection.cursor()
        # DDL is isolated before all store transactions because MySQL DDL commits
        # implicitly. Only this module's fixed, newly authorized table is used.
        cursor.execute(TABLE_META_SQL, (TABLE,))
        if cursor.fetchone() is None:
            cursor.execute(CREATE_SQL)
            try:
                connection.commit()
            except Exception:
                return _commit_unknown()
        if not _schema_owned(cursor):
            return _failure("table_schema_or_ownership_mismatch")
        for store, fetch_status, rows, status_row in prepared:
            try:
                connection.begin()
                if fetch_status == "success":
                    # Only complete, validated windows reach this operation.
                    for window_key in WINDOWS:
                        cursor.execute(RETIRE_SQL, (attempt, store, window_key))
                    if rows:
                        cursor.executemany(UPSERT_SQL, [_bound_row(row) for row in rows])
                else:
                    cursor.execute(STALE_SQL, (attempt, store))
                cursor.execute(UPSERT_SQL, _bound_row(status_row))
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                return _failure("database_write_failed", written, committed)
            # A lost commit acknowledgement may follow a successful server
            # commit. Rolling back cannot establish which outcome occurred.
            try:
                connection.commit()
            except Exception:
                return _commit_unknown(written, committed)
            written += len(rows) + 1
            committed += 1
        return {"status": "updated", "table": TABLE, "rows_written": written, "stores": committed}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        return _failure("database_write_failed", written, committed)
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
