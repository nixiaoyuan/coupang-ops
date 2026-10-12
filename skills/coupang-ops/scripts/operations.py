"""Five read-only operations answers from normalized, store-scoped exports.

Python 3.10 standard library only. ``analysis_date`` is the Seoul analysis day;
all sales / shipment / transaction windows end on the preceding complete day.
The caller supplies source completeness explicitly. Missing records are unknown,
unless a complete sales window explicitly confirms that zero rows are omitted.
No files, credentials, network services, or source datasets are modified here.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


SEOUL = timezone(timedelta(hours=9), "Asia/Seoul")
DEFAULT_THRESHOLDS = {
    "previous_min_units": 20,
    "drop_fraction": ".30",
    "stock_days": "7",
    "listed_days": 30,
    "return_min_shipped": 20,
    "return_fraction": ".10",
}
TRAFFIC_FIELDS = (
    "totalUniqueVisitor", "totalPageViews", "srpClick", "searchVolume", "pvToOrder",
)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: 必须是非空字符串")
    return value.strip()


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label}: 必须是 YYYY-MM-DD 日期")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label}: 无效日期") from error


def _timestamp(value, label):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value,
    ):
        raise ValueError(f"{label}: 必须是带 UTC 偏移的 ISO 时间，不能用未知时区或日期代替")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label}: 无效时间") from error
    if result.utcoffset() is None:
        raise ValueError(f"{label}: 时间缺少 UTC 偏移")
    return result


def _count(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}: 必须是非负整数，不能用净销量代替实际件数")
    return value


def _decimal(value, label, require_string=False, signed=False):
    if isinstance(value, bool) or (require_string and not isinstance(value, str)):
        raise ValueError(f"{label}: 必须是有限非负十进制数" + ("字符串" if require_string else ""))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{label}: 无效十进制数") from error
    if not result.is_finite() or (result < 0 and not signed):
        raise ValueError(f"{label}: 必须是有限非负十进制数")
    return result


def _ratio(numerator, denominator):
    return str(Decimal(numerator) / Decimal(denominator)) if denominator else None


def _section(status="ok", missing=(), rows=(), **details):
    return {"status": status, "missing": sorted(set(missing)), "rows": list(rows), **details}


def _context(row, dataset, label):
    if not isinstance(row, dict):
        raise ValueError(f"{label}: 必须是对象")
    for key in ("store", "timezone", "currency"):
        if key in row and row[key] != dataset[key]:
            raise ValueError(f"{label}: 不允许混合店铺、时区或币种 ({key})")


def _source(extras, key, dataset):
    rows = extras.get(key)
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise ValueError(f"extras.{key}: 必须是列表")
    for index, row in enumerate(rows):
        _context(row, dataset, f"extras.{key}[{index}]")
    return rows


def _unique(rows, key, label):
    result = {}
    for row in rows or []:
        identity = _text(row.get(key), f"{label}.{key}")
        if identity in result:
            raise ValueError(f"{label}: 重复 {key} {identity}")
        result[identity] = row
    return result


def _coverage(coverage, source, start=None, end=None):
    if coverage.get(f"{source}_complete") is not True:
        return False
    if start is None:
        return True
    first, last = coverage.get(f"{source}_start_date"), coverage.get(f"{source}_end_date")
    return bool(first and last and _date(first, source) <= start and _date(last, source) >= end)


def _thresholds(values):
    result = dict(DEFAULT_THRESHOLDS)
    if values is not None:
        if not isinstance(values, dict) or set(values) - set(result):
            raise ValueError("thresholds: 含不支持的阈值")
        result.update(values)
    for key in ("previous_min_units", "listed_days", "return_min_shipped"):
        _count(result[key], f"thresholds.{key}")
        if not result[key]:
            raise ValueError(f"thresholds.{key}: 必须大于零")
    for key in ("drop_fraction", "stock_days", "return_fraction"):
        value = _decimal(result[key], f"thresholds.{key}")
        if key.endswith("fraction") and value > 1:
            raise ValueError(f"thresholds.{key}: 必须在 0 到 1 之间")
        result[key] = str(value)
    return result


def _windows(dataset, analysis_day):
    supplied = dataset.get("sales_windows", {})
    if not isinstance(supplied, dict):
        raise ValueError("sales_windows: 必须是对象")
    last = analysis_day - timedelta(days=1)
    bounds = {
        "last_30d": (analysis_day - timedelta(days=30), last),
        "current_7d": (analysis_day - timedelta(days=7), last),
        "previous_7d": (analysis_day - timedelta(days=14), analysis_day - timedelta(days=8)),
    }
    parsed = {}
    for name, (start, end) in bounds.items():
        raw = supplied.get(name)
        if raw is None:
            parsed[name] = {"start_date": str(start), "end_date": str(end), "complete": False,
                            "present": False, "zero_row_omission_confirmed": False, "rows": {}}
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"sales_windows.{name}: 必须是对象")
        if (_date(raw.get("start_date"), name), _date(raw.get("end_date"), name)) != (start, end):
            raise ValueError(f"sales_windows.{name}: 日期必须是最近完整的 {start} 至 {end}")
        if type(raw.get("complete")) is not bool:
            raise ValueError(f"sales_windows.{name}.complete: 必须明确 true / false")
        zero_omission = raw.get("zero_row_omission_confirmed", False)
        if type(zero_omission) is not bool:
            raise ValueError(f"sales_windows.{name}.zero_row_omission_confirmed: 必须为布尔值")
        if not isinstance(raw.get("rows"), list):
            raise ValueError(f"sales_windows.{name}.rows: 必须是列表")
        rows = {}
        for raw_row in raw["rows"]:
            _context(raw_row, dataset, name)
            sku = _text(raw_row.get("sku"), f"{name}.sku")
            if sku in rows:
                raise ValueError(f"{name}: 重复 SKU {sku}")
            traffic = raw_row.get("traffic", {})
            if traffic is None:
                traffic = {}
            if not isinstance(traffic, dict):
                raise ValueError(f"{name}.{sku}.traffic: 必须是对象")
            normalized_traffic = {}
            for key in TRAFFIC_FIELDS:
                if key in traffic and traffic[key] is not None:
                    normalized_traffic[key] = str(_decimal(traffic[key], f"{name}.{sku}.{key}", signed=key == "pvToOrder"))
            sales_units = raw_row.get("units")
            if type(sales_units) is not int:
                raise ValueError(f"{name}.{sku}.units: 后台汇总销量必须是整数")
            rows[sku] = {
                "sku": sku, "name": _text(raw_row.get("name"), f"{name}.{sku}.name"),
                "units": sales_units,
                "amount": str(_decimal(raw_row.get("amount"), f"{name}.{sku}.amount", True, signed=True)),
                "traffic": normalized_traffic,
            }
        parsed[name] = {"start_date": str(start), "end_date": str(end),
                        "complete": raw["complete"], "present": True,
                        "zero_row_omission_confirmed": zero_omission, "rows": rows}
    return parsed


def _ranking(window, basis):
    rows = [dict(row) for row in window["rows"].values()]
    complete = window["complete"]
    total_units = sum(row["units"] for row in rows)
    total_amount = sum((Decimal(row["amount"]) for row in rows), Decimal(0))
    for row in rows:
        row["units_share"] = _ratio(row["units"], total_units) if complete and total_units > 0 else None
        row["amount_share"] = _ratio(Decimal(row["amount"]), total_amount) if complete and total_amount > 0 else None
    units = sorted(rows, key=lambda row: (-row["units"], row["sku"]))
    amounts = sorted(rows, key=lambda row: (-Decimal(row["amount"]), row["sku"]))
    status = "ok" if complete else "incomplete" if window["present"] else "missing_data"
    return _section(
        status, [] if complete else ["sales_windows.last_30d.full_store_complete"], units,
        top_units=units[:10], top_amount=amounts[:10],
        units_label="净销量" if basis == "net_confirmed" else "平台销量",
        amount_label="净销售额" if basis == "net_confirmed" else "平台销售额 / GMV",
        denominator={"units": total_units if complete else None,
                     "amount": str(total_amount) if complete else None,
                     "observed_units": total_units, "observed_amount": str(total_amount),
                     "scope": "all_store_full_window" if complete else "partial_observed_rows",
                     "complete": complete, "sku_count": len(rows)},
    )


def _sales_drop(windows, thresholds, known_names=None):
    previous, current = windows["previous_7d"], windows["current_7d"]
    missing = []
    for name, window in (("previous_7d", previous), ("current_7d", current)):
        if not window["complete"]:
            missing.append(f"sales_windows.{name}.full_store_complete")
    rows = []
    known_names = known_names or {}
    known_skus = set(previous["rows"]) | set(current["rows"]) | set(windows["last_30d"]["rows"]) | set(known_names)
    for sku in sorted(known_skus):
        old, new = previous["rows"].get(sku), current["rows"].get(sku)
        old_units = old["units"] if old else 0 if previous["complete"] and previous["zero_row_omission_confirmed"] else None
        new_units = new["units"] if new else 0 if current["complete"] and current["zero_row_omission_confirmed"] else None
        if old_units is None:
            missing.append(f"previous_7d.{sku}.explicit_row_or_confirmed_zero_omission")
        if new_units is None:
            missing.append(f"current_7d.{sku}.explicit_row_or_confirmed_zero_omission")
        comparable = previous["complete"] and current["complete"] and old_units is not None and new_units is not None
        change = new_units - old_units if old_units is not None and new_units is not None else None
        decline = _ratio(old_units - new_units, old_units) if old_units is not None and old_units > 0 and new_units is not None else None
        alert = (old_units >= thresholds["previous_min_units"] and Decimal(decline) > Decimal(thresholds["drop_fraction"])) if comparable and old_units else False if comparable else None
        rows.append({"sku": sku, "name": (new or old)["name"] if new or old else known_names.get(sku, windows["last_30d"]["rows"].get(sku, {}).get("name", sku)), "previous_units": old_units,
                     "current_units": new_units, "change_units": change, "drop_fraction": decline,
                     "comparison_complete": comparable, "alert": alert,
                     "previous_traffic": old["traffic"] if old else None,
                     "current_traffic": new["traffic"] if new else None})
    status = "missing_data" if not previous["present"] or not current["present"] else "incomplete" if missing else "ok"
    return _section(status, missing, rows, traffic_note="流量和转化仅作描述；无法据此确定销量下滑原因。")


def _extras(extras, dataset, observed):
    if extras is None:
        extras = {}
    if not isinstance(extras, dict):
        raise ValueError("extras: 必须是同一店铺的数据对象")
    _context(extras, dataset, "extras")
    coverage = extras.get("coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("extras.coverage: 必须是对象")
    for key, value in coverage.items():
        if key.endswith("_complete") and type(value) is not bool:
            raise ValueError(f"coverage.{key}: 必须明确 true / false")
    for source in ("shipments", "transactions"):
        start, end = coverage.get(f"{source}_start_date"), coverage.get(f"{source}_end_date")
        if start is not None:
            _date(start, source)
        if end is not None:
            _date(end, source)
        if start and end and start > end:
            raise ValueError(f"coverage.{source}: 起始日不能晚于结束日")
    products = _unique(_source(extras, "products", dataset), "sku", "products")
    inventory = _unique(_source(extras, "inventory", dataset), "sku", "inventory")
    shipments = _unique(_source(extras, "shipments", dataset), "shipment_line_id", "shipments")
    returns = _unique(_source(extras, "returns", dataset), "return_line_id", "returns")
    transactions = _unique(_source(extras, "transactions", dataset), "transaction_line_id", "transactions")
    for sku, row in products.items():
        _text(row.get("name"), f"products.{sku}.name")
        row_first = _date(row.get("first_listed_at"), f"products.{sku}.first_listed_at")
        if row_first > observed.astimezone(SEOUL).date():
            raise ValueError(f"products.{sku}: 首次上架日期晚于观察截止")
        if row.get("last_deal_at") is not None:
            last = _date(row["last_deal_at"], f"products.{sku}.last_deal_at")
            if last < row_first or last > observed.astimezone(SEOUL).date():
                raise ValueError(f"products.{sku}: 末次成交日期超出上架至观察截止范围")
        if row.get("unit_cost") is not None:
            _decimal(row["unit_cost"], f"products.{sku}.unit_cost", True)
    for sku, row in inventory.items():
        _count(row.get("available_units"), f"inventory.{sku}.available_units")
        snapshot = _timestamp(row.get("snapshot_at"), f"inventory.{sku}.snapshot_at")
        if snapshot > observed:
            raise ValueError(f"inventory.{sku}: 库存快照晚于观察截止时间")
    for identity, row in shipments.items():
        _text(row.get("order_id"), f"shipments.{identity}.order_id")
        _text(row.get("sku"), f"shipments.{identity}.sku")
        _timestamp(row.get("shipped_at"), f"shipments.{identity}.shipped_at")
        _count(row.get("shipped_units"), f"shipments.{identity}.shipped_units")
    returned_by_shipment = Counter()
    for identity, row in returns.items():
        shipment_id = _text(row.get("shipment_line_id"), f"returns.{identity}.shipment_line_id")
        completed = _timestamp(row.get("completed_at"), f"returns.{identity}.completed_at")
        _count(row.get("returned_units"), f"returns.{identity}.returned_units")
        if row.get("reason") is not None and not isinstance(row["reason"], str):
            raise ValueError(f"returns.{identity}.reason: 必须是字符串或 null")
        if shipment_id in shipments:
            shipment = shipments[shipment_id]
            if completed < _timestamp(shipment["shipped_at"], "shipped_at"):
                raise ValueError(f"returns.{identity}: 完成退货早于实际发货")
            returned_by_shipment[shipment_id] += row["returned_units"]
            if returned_by_shipment[shipment_id] > shipment["shipped_units"]:
                raise ValueError(f"returns.{identity}: 累计退货件数超过对应发货行")
    for identity, row in transactions.items():
        _text(row.get("sku"), f"transactions.{identity}.sku")
        _timestamp(row.get("occurred_at"), f"transactions.{identity}.occurred_at")
        _count(row.get("units"), f"transactions.{identity}.units")
    return extras, coverage, products, inventory, shipments, returns, transactions


def _stock(extras, coverage, inventory, shipments, names, analysis_day, thresholds):
    start, end = analysis_day - timedelta(days=14), analysis_day - timedelta(days=1)
    missing = []
    for source in ("inventory", "shipments"):
        if extras.get(source) is None:
            missing.append(source)
        elif not _coverage(coverage, source, start if source == "shipments" else None, end):
            missing.append(f"coverage.{source}_complete" + ("_14_days" if source == "shipments" else ""))
    shipment_complete = extras.get("shipments") is not None and _coverage(coverage, "shipments", start, end)
    units = Counter()
    for row in shipments.values():
        if start <= _timestamp(row["shipped_at"], "shipped_at").astimezone(SEOUL).date() <= end:
            units[row["sku"]] += row["shipped_units"]
    rows = []
    for sku in sorted(set(inventory) | set(names)):
        row = inventory.get(sku)
        if row is None:
            missing.append(f"inventory.{sku}.explicit_snapshot")
            rows.append({"sku": sku, "name": names.get(sku, sku), "available_units": None,
                         "snapshot_at": None, "shipped_units_14d": units[sku] if shipment_complete else None,
                         "observed_shipped_units_14d": units[sku], "daily_shipped_units": str(Decimal(units[sku]) / 14) if shipment_complete else None,
                         "coverage_days": None, "alert": None, "assessment": "missing_inventory"})
            continue
        demand = Decimal(units[sku]) / 14 if shipment_complete else None
        days = Decimal(row["available_units"]) / demand if demand else None
        rows.append({"sku": sku, "name": names.get(sku, sku), "available_units": row["available_units"],
                     "snapshot_at": row["snapshot_at"], "shipped_units_14d": units[sku] if shipment_complete else None,
                     "observed_shipped_units_14d": units[sku], "daily_shipped_units": str(demand) if demand is not None else None,
                     "coverage_days": str(days) if days is not None else None,
                     "alert": days < Decimal(thresholds["stock_days"]) if days is not None else None,
                     "assessment": "unknown_demand" if shipment_complete and not demand else "incomplete_shipments" if not shipment_complete else "low_stock" if days < Decimal(thresholds["stock_days"]) else "threshold_not_triggered"})
    status = "missing_data" if any(extras.get(source) is None for source in ("inventory", "shipments")) else "incomplete" if missing else "ok"
    return _section(status, missing, rows, demand_window={"start_date": str(start), "end_date": str(end)},
                    formula="可售库存 ÷ (最近14个完整日的实际发货件数 ÷ 14)")


def _slow(extras, coverage, products, inventory, transactions, names, analysis_day, thresholds):
    start, end = analysis_day - timedelta(days=30), analysis_day - timedelta(days=1)
    missing = []
    for source in ("products", "inventory", "transactions"):
        if extras.get(source) is None:
            missing.append(source)
        elif not _coverage(coverage, source, start if source == "transactions" else None, end):
            missing.append(f"coverage.{source}_complete" + ("_30_days" if source == "transactions" else ""))
    for sku in set(names) - set(inventory):
        missing.append(f"inventory.{sku}.explicit_snapshot")
    transaction_complete = extras.get("transactions") is not None and _coverage(coverage, "transactions", start, end)
    units = Counter()
    for row in transactions.values():
        if start <= _timestamp(row["occurred_at"], "occurred_at").astimezone(SEOUL).date() <= end:
            units[row["sku"]] += row["units"]
    rows = []
    for sku, stock in sorted(inventory.items()):
        if stock["available_units"] <= 0:
            continue
        product = products.get(sku)
        if not product:
            missing.append(f"products.{sku}.first_listed_at")
        listed_days = (analysis_day - _date(product["first_listed_at"], "first_listed_at")).days if product else None
        eligible = listed_days >= thresholds["listed_days"] if listed_days is not None else None
        alert = bool(eligible and units[sku] == 0) if eligible is not None and transaction_complete else None
        rows.append({"sku": sku, "name": names.get(sku, sku), "available_units": stock["available_units"],
                     "snapshot_at": stock["snapshot_at"], "first_listed_at": product["first_listed_at"] if product else None,
                     "listed_days": listed_days, "eligible": eligible,
                     "non_cancelled_transaction_units_30d": units[sku] if transaction_complete else None,
                     "observed_transaction_units_30d": units[sku], "alert": alert,
                     "last_deal_at": product.get("last_deal_at") if product else None,
                     "last_deal_status": "known" if product and product.get("last_deal_at") else "unknown",
                     "unit_cost": product.get("unit_cost") if product else None})
    status = "missing_data" if any(extras.get(source) is None for source in ("products", "inventory", "transactions")) else "incomplete" if missing else "ok"
    return _section(status, missing, rows, transaction_window={"start_date": str(start), "end_date": str(end)},
                    note="零成交必须来自完整的非取消成交记录；净销量为零不能证明零成交，末次成交日期缺失时保留未知。")


def _returns(extras, coverage, shipments, returns, names, analysis_day, observed, thresholds):
    start, end = analysis_day - timedelta(days=30), analysis_day - timedelta(days=1)
    missing = []
    for source in ("shipments", "returns"):
        if extras.get(source) is None:
            missing.append(source)
        elif not _coverage(coverage, source, start if source == "shipments" else None, end):
            missing.append(f"coverage.{source}_complete" + ("_30_days" if source == "shipments" else ""))
    cohort = {identity: row for identity, row in shipments.items()
              if start <= _timestamp(row["shipped_at"], "shipped_at").astimezone(SEOUL).date() <= end}
    shipped, returned, unknown_units, unknown_count = Counter(), Counter(), Counter(), Counter()
    reasons = {}
    for row in cohort.values():
        shipped[row["sku"]] += row["shipped_units"]
    ignored_after_cutoff, outside_cohort = 0, 0
    for identity, row in returns.items():
        if _timestamp(row["completed_at"], "completed_at") > observed:
            ignored_after_cutoff += 1
            continue
        shipment_id = row["shipment_line_id"]
        if shipment_id not in shipments:
            missing.append(f"returns.{identity}.matching_shipment_line")
            continue
        if shipment_id not in cohort:
            outside_cohort += 1
            continue
        sku = cohort[shipment_id]["sku"]
        returned[sku] += row["returned_units"]
        reason = row.get("reason")
        if not reason or not reason.strip():
            unknown_units[sku] += row["returned_units"]
            unknown_count[sku] += 1
        else:
            reasons.setdefault(sku, Counter())[reason.strip()] += row["returned_units"]
    complete = not missing
    rows = []
    for sku in sorted(shipped):
        rate = _ratio(returned[sku], shipped[sku]) if complete else None
        rows.append({"sku": sku, "name": names.get(sku, sku), "shipped_units": shipped[sku] if complete else None,
                     "observed_shipped_units": shipped[sku], "returned_units": returned[sku] if complete else None,
                     "observed_returned_units": returned[sku], "return_fraction": rate,
                     "alert": shipped[sku] >= thresholds["return_min_shipped"] and Decimal(rate) > Decimal(thresholds["return_fraction"]) if rate is not None else None,
                     "reason_distribution_units": dict(sorted(reasons.get(sku, {}).items())),
                     "unknown_reason_units": unknown_units[sku], "unknown_reason_count": unknown_count[sku]})
    status = "missing_data" if any(extras.get(source) is None for source in ("shipments", "returns")) else "incomplete" if missing else "provisional"
    return _section(status, missing, rows, cohort_window={"start_date": str(start), "end_date": str(end)},
                    observation_cutoff=observed.isoformat(), ignored_after_cutoff=ignored_after_cutoff,
                    ignored_outside_cohort=outside_cohort,
                    note="按实际发货行归属最近30天发货批次，累计观察截止前完成的退货；近期批次观察期较短，比例尚未成熟。")


def analyze_store(dataset, extras=None, thresholds=None):
    """Return JSON-compatible results; reject malformed data, preserve unknowns."""
    if not isinstance(dataset, dict):
        raise ValueError("dataset: 必须是单店铺对象")
    _text(dataset.get("store"), "store")
    if dataset.get("timezone") != "Asia/Seoul" or dataset.get("currency") != "KRW":
        raise ValueError("必须明确单店铺 Asia/Seoul 时区及 KRW 币种")
    analysis_day = _date(dataset.get("analysis_date"), "analysis_date")
    observed = _timestamp(dataset.get("observed_at"), "observed_at")
    if observed.astimezone(SEOUL).date() < analysis_day:
        raise ValueError("observed_at 早于 analysis_date，最近完整日窗口尚未结束")
    basis = dataset.get("sales_basis")
    if basis not in ("platform_reported", "net_confirmed"):
        raise ValueError("sales_basis: 必须是 platform_reported 或 net_confirmed")
    _text(dataset.get("sales_basis_note"), "sales_basis_note")
    limits = _thresholds(thresholds)
    windows = _windows(dataset, analysis_day)
    parsed = _extras(extras, dataset, observed)
    extras, coverage, products, inventory, shipments, returns, transactions = parsed
    names = {sku: row["name"] for window in windows.values() for sku, row in window["rows"].items()}
    names.update({sku: row["name"] for sku, row in products.items()})
    for row in list(shipments.values()) + list(transactions.values()):
        names.setdefault(row["sku"], row["sku"])
    result = {key: dataset[key] for key in ("store", "timezone", "currency", "analysis_date", "observed_at", "sales_basis", "sales_basis_note")}
    result.update({"last_complete_date": str(analysis_day - timedelta(days=1)), "thresholds": limits,
                   "sales_windows": {name: {key: window[key] for key in ("start_date", "end_date", "complete", "present", "zero_row_omission_confirmed")} for name, window in windows.items()},
                   "ranking": _ranking(windows["last_30d"], basis),
                   "sales_drop": _sales_drop(windows, limits, names),
                   "stock_risk": _stock(extras, coverage, inventory, shipments, names, analysis_day, limits),
                   "slow_stock": _slow(extras, coverage, products, inventory, transactions, names, analysis_day, limits),
                   "returns": _returns(extras, coverage, shipments, returns, names, analysis_day, observed, limits),
                   "limitations": ["销售额和销量排名不能代表利润；未确认净口径时沿用平台销量和平台销售额 / GMV。",
                                   "完整性依赖源数据覆盖声明；缺失值不等于零，未知成本不按零处理。",
                                   "流量和转化仅作描述，不能据此确定销量变化原因。",
                                   "库存覆盖是历史实际发货速度的估计，零需求无法估算；未包括在途、供货周期和未来需求变化。",
                                   "退货按实际发货批次和固定观察截止匹配，近期批次退货比例尚未成熟。"]})
    return result


def _safe(value):
    result = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    for character in ("\\", "|", "`", "[", "]", "*", "_"):
        result = result.replace(character, "\\" + character)
    return result


def _percent(value):
    return f"{Decimal(value) * 100:.1f}%" if value is not None else "未知"


def _number(value, places=1):
    return f"{Decimal(value):,.{places}f}" if value is not None else "未知"


def _state(section):
    return {"ok": "数据范围完整", "provisional": "固定截止的暂定观察", "missing_data": "缺少必要数据，不能作完整判断", "incomplete": "覆盖不完整，不能作完整判断"}[section["status"]]


def render_report(summaries):
    """Render five Chinese answers per store, with snapshot and cutoff evidence."""
    if not isinstance(summaries, list):
        raise ValueError("render_report 需要单店铺分析结果列表")
    lines = ["# Coupang 五项运营分析", ""]
    if not summaries:
        lines.extend(["未提供店铺数据，无法分析。", ""])
    for summary in summaries:
        thresholds = summary["thresholds"]
        lines.extend([f"## 店铺：{_safe(summary['store'])}", "",
                      f"分析日期：{summary['analysis_date']}；最近完整日：{summary['last_complete_date']}；时区：Asia/Seoul；币种：KRW。",
                      f"观察截止：{_safe(summary['observed_at'])}。销售口径：{_safe(summary['sales_basis_note'])}。", ""])
        ranking = summary["ranking"]
        window = summary["sales_windows"]["last_30d"]
        lines.extend(["### 1. 最近30天哪些商品卖得最多、销售额最高？", "",
                      f"{window['start_date']} 至 {window['end_date']}；{_state(ranking)}。两项排名分别取前10。",
                      "占比的分母为同店同期间全部商品；全店数据不完整时占比未知。", ""])
        for key, label, metric, share in (("top_units", ranking["units_label"], "units", "units_share"),
                                          ("top_amount", ranking["amount_label"], "amount", "amount_share")):
            lines.extend([f"**按{label}排序**", "", f"| SKU / 商品 | {label} | 全店占比 |", "| --- | ---: | ---: |"])
            for row in ranking[key]:
                value = str(row[metric]) if metric == "units" else _number(row[metric], 2) + " KRW"
                lines.append(f"| {_safe(row['sku'])} / {_safe(row['name'])} | {value} | {_percent(row[share])} |")
            if not ranking[key]:
                lines.append("| 未取得可排名记录 | 未知 | 未知 |")
            lines.append("")
        for key, title in (("sales_drop", "2. 哪些商品销量明显下滑？"),
                            ("stock_risk", "3. 哪些商品库存覆盖不足？"),
                            ("slow_stock", "4. 哪些在库商品长期没有成交？"),
                            ("returns", "5. 哪些商品退货比例偏高？")):
            section = summary[key]
            lines.extend([f"### {title}", "", _state(section) + "。"])
            for missing in section["missing"]:
                lines.append(f"- 缺少或未确认：{_safe(missing)}。")
            if key == "sales_drop":
                old_window, new_window = summary["sales_windows"]["previous_7d"], summary["sales_windows"]["current_7d"]
                lines.append(f"上期：{old_window['start_date']} 至 {old_window['end_date']}；本期：{new_window['start_date']} 至 {new_window['end_date']}，均为7个完整日。")
                lines.append(f"规则：上期7天销量 ≥ {thresholds['previous_min_units']} 件，且本期7天降幅 > {_percent(thresholds['drop_fraction'])}。")
                for row in section["rows"]:
                    if row["alert"] or not row["comparison_complete"]:
                        lines.append(f"- {_safe(row['name'])}（{_safe(row['sku'])}）：上期 {row['previous_units'] if row['previous_units'] is not None else '未知'} 件，本期 {row['current_units'] if row['current_units'] is not None else '未知'} 件；降幅 {_percent(row['drop_fraction'])}；{'触发阈值' if row['alert'] else '比较不完整'}。")
                lines.append(section["traffic_note"])
            elif key == "stock_risk":
                demand_window = section["demand_window"]
                lines.append(f"实际发货需求期间：{demand_window['start_date']} 至 {demand_window['end_date']}。")
                lines.append(section["formula"] + f"；阈值 < {thresholds['stock_days']} 天。")
                for row in section["rows"]:
                    detail = "库存快照缺失，无法估算" if row["assessment"] == "missing_inventory" else "零发货需求，无法估算" if row["assessment"] == "unknown_demand" else "实际发货覆盖不足，无法估算" if row["assessment"] == "incomplete_shipments" else "触发阈值" if row["alert"] else "未触发阈值"
                    lines.append(f"- {_safe(row['name'])}（{_safe(row['sku'])}）：可售 {row['available_units'] if row['available_units'] is not None else '未知'} 件；库存快照 {_safe(row['snapshot_at']) if row['snapshot_at'] else '未知'}；覆盖 {_number(row['coverage_days'])} 天；{detail}。")
            elif key == "slow_stock":
                transaction_window = section["transaction_window"]
                lines.append(f"非取消成交期间：{transaction_window['start_date']} 至 {transaction_window['end_date']}。")
                lines.append(f"规则：在库、上架 ≥ {thresholds['listed_days']} 天，且最近30个完整日非取消成交件数为零。")
                for row in section["rows"]:
                    if row["alert"] or row["alert"] is None:
                        lines.append(f"- {_safe(row['name'])}（{_safe(row['sku'])}）：可售 {row['available_units']} 件；库存快照 {_safe(row['snapshot_at'])}；上架 {row['listed_days'] if row['listed_days'] is not None else '未知'} 天；成交 {row['non_cancelled_transaction_units_30d'] if row['non_cancelled_transaction_units_30d'] is not None else '未知'} 件；末次成交日期 {_safe(row['last_deal_at']) if row['last_deal_at'] else '未知'}；{'触发阈值' if row['alert'] else '数据不足'}。")
                lines.append(section["note"])
            else:
                cohort = section["cohort_window"]
                lines.append(f"发货批次：{cohort['start_date']} 至 {cohort['end_date']}；退货观察截止：{_safe(section['observation_cutoff'])}。规则：同批次发货 ≥ {thresholds['return_min_shipped']} 件，已完成退货件数 / 实际发货件数 > {_percent(thresholds['return_fraction'])}。")
                for row in section["rows"]:
                    lines.append(f"- {_safe(row['name'])}（{_safe(row['sku'])}）：完成退货 {row['returned_units'] if row['returned_units'] is not None else '未知'} / 发货 {row['shipped_units'] if row['shipped_units'] is not None else '未知'} 件 = {_percent(row['return_fraction'])}；{'触发阈值' if row['alert'] else '数据不足' if row['alert'] is None else '未触发阈值'}；已知原因 {_safe(row['reason_distribution_units'])}；原因未知 {row['unknown_reason_units']} 件（{row['unknown_reason_count']} 条）。")
                lines.append(section["note"])
            if section["status"] in ("ok", "provisional") and not any(row.get("alert") for row in section["rows"]) and not any(row.get("alert") is None for row in section["rows"]):
                lines.append("已覆盖的完整范围内没有商品触发此阈值。")
            lines.append("")
        lines.extend(["**口径和限制**", ""])
        lines.extend(f"- {_safe(note)}" for note in summary["limitations"])
        lines.append("")
    return "\n".join(lines)
