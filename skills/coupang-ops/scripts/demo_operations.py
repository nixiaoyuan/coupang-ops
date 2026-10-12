"""Deterministic synthetic inputs for the five-question, four-store demonstration.

All records are generated locally. No captured user data, credentials, or live
services are used. Prices, fees, refunds, and costs are decimal strings. Explicit
zero sales rows and complete non-cancelled transaction coverage distinguish true
zero activity from missing records or net sales after returns.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any


SEOUL = timezone(timedelta(hours=9), "Asia/Seoul")
_PRODUCTS = (
    ("DEMO-LOW", "演示商品：热销低库存", "12500.10", "4500.25", 8),
    ("DEMO-DROP", "演示商品：近期销量下降", "48000.25", "17500.50", 100),
    ("DEMO-ZERO", "演示商品：在库近30天零成交", "8900.00", "3200.00", 20),
    ("DEMO-RETURN", "演示商品：部分退货偏高", "19000.75", "7800.40", 80),
    ("DEMO-NEW", "演示商品：近期上架", "25000.50", "9000.30", 30),
)
_BASIS_NOTE = (
    "全部为本地生成的合成演示数据，不能作为真实店铺结论。"
    "销售示例采用平台销量 / GMV口径，退货单独按实际发货行归属，未从销售额重复扣减。"
)


def _stamp(day: date, hour: int = 12, minute: int = 0) -> str:
    return datetime.combine(day, time(hour, minute), SEOUL).isoformat(timespec="seconds")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _units(sku: str, days_before_analysis: int, multiplier: int) -> int:
    if sku == "DEMO-LOW":
        units = 8 if days_before_analysis <= 7 else 6 if days_before_analysis <= 14 else 4
    elif sku == "DEMO-DROP":
        units = 2 if days_before_analysis <= 7 else 5 if days_before_analysis <= 14 else 3
    elif sku == "DEMO-ZERO":
        units = 0
    elif sku == "DEMO-RETURN":
        units = 2
    else:
        # This SKU first appeared ten days ago, so preceding days are confirmed
        # zero activity rather than sales before the listing date.
        units = 2 if days_before_analysis <= 7 else 1 if days_before_analysis <= 10 else 0
    return units * multiplier


def _window_rows(analysis_day: date, start: date, end: date, multiplier: int) -> list[dict[str, Any]]:
    day_count = (end - start).days + 1
    rows = []
    for sku, name, price, _cost, _stock in _PRODUCTS:
        total = sum(_units(sku, (analysis_day - (start + timedelta(days=index))).days, multiplier)
                    for index in range(day_count))
        visits = total * 3 + day_count * 10
        page_views = visits * 2
        orders = total // 2 + total % 2
        rows.append({
            "sku": sku, "name": name, "units": total,
            "amount": _money(Decimal(price) * total),
            "traffic": {
                "totalOrders": orders, "totalUniqueVisitor": visits,
                "totalPageViews": page_views, "srpClick": visits // 2,
                "searchVolume": day_count * 100 * multiplier,
                "pvToOrder": str(Decimal(orders) / Decimal(page_views)),
            },
            "registration_type": "RFM" if sku == "DEMO-RETURN" else "NORMAL",
            "is_oos": False, "inventory_id": "SYNTHETIC-INV-" + sku,
        })
    return rows


def _raw_rows(store: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "vendorItemDetails": {
            "vendorId": store, "vendorItemId": row["sku"], "itemName": row["name"],
            "productName": row["name"], "registrationType": row["registration_type"],
            "isOOS": row["is_oos"], "inventoryId": row["inventory_id"],
        },
        "businessInsightsMetricsResponse": {
            "totalUnitsSold": row["units"], "totalGmv": row["amount"], **row["traffic"],
        },
    } for row in rows]


def _store_extras(store: str, analysis_day: date, multiplier: int) -> dict[str, Any]:
    first = analysis_day - timedelta(days=30)
    last = analysis_day - timedelta(days=1)
    context = {"store": store, "timezone": "Asia/Seoul", "currency": "KRW"}
    products, inventory, shipments, transactions, returns = [], [], [], [], []
    for sku, name, price, cost, stock in _PRODUCTS:
        products.append({
            **context, "sku": sku, "name": name,
            "first_listed_at": str(analysis_day - timedelta(days=10 if sku == "DEMO-NEW" else 120)),
            "last_deal_at": str(analysis_day - timedelta(days=45 if sku == "DEMO-ZERO" else 1)),
            "unit_cost": cost, "data_kind": "synthetic_demo",
        })
        inventory.append({
            **context, "sku": sku, "available_units": stock * multiplier,
            "snapshot_at": _stamp(analysis_day, 9, 45), "data_kind": "synthetic_demo",
        })
        for days_before in range(30, 0, -1):
            units = _units(sku, days_before, multiplier)
            if not units:
                continue
            day = analysis_day - timedelta(days=days_before)
            identity = f"{store}-{sku}-{day.isoformat()}"
            gross = Decimal(price) * units
            shipments.append({
                **context, "shipment_line_id": "DEMO-S-" + identity,
                "order_id": "DEMO-O-" + identity, "sku": sku,
                "shipped_at": _stamp(day, 16), "shipped_units": units,
                "shipping_fee": _money(Decimal("500.00") * multiplier),
                "data_kind": "synthetic_demo",
            })
            transactions.append({
                **context, "transaction_line_id": "DEMO-T-" + identity, "sku": sku,
                "occurred_at": _stamp(day, 12), "units": units,
                "gross_amount": _money(gross),
                "platform_fee": _money(gross * Decimal("0.08")),
                "data_kind": "synthetic_demo",
            })
            # Eight older shipment lines each have half of their units returned.
            # This is 8*m / 60*m = 13.33% of this SKU's thirty-day shipment cohort.
            # Every return matches a real synthetic shipment line and is complete
            # before the observation cutoff; there are no full-return/net-zero
            # shortcuts behind the zero-transaction example.
            if sku == "DEMO-RETURN" and 13 <= days_before <= 20:
                reason = "尺寸不符" if days_before <= 17 else "破损" if days_before <= 19 else None
                returns.append({
                    **context, "return_line_id": "DEMO-R-" + identity,
                    "shipment_line_id": "DEMO-S-" + identity,
                    "returned_units": multiplier,
                    "completed_at": _stamp(day + timedelta(days=3), 15),
                    "reason": reason, "refund_amount": _money(Decimal(price) * multiplier),
                    "data_kind": "synthetic_demo",
                })
    return {
        **context, "data_kind": "synthetic_demo",
        "coverage": {
            "products_complete": True, "inventory_complete": True,
            "shipments_complete": True, "shipments_start_date": str(first),
            "shipments_end_date": str(last), "returns_complete": True,
            "transactions_complete": True, "transactions_start_date": str(first),
            "transactions_end_date": str(last),
        },
        "products": products, "inventory": inventory, "shipments": shipments,
        "returns": returns, "transactions": transactions,
    }


def build_demo_bundle(analysis_date: str = "2026-10-12") -> dict[str, Any]:
    """Return four reproducible, explicitly synthetic store datasets and extras."""
    if not isinstance(analysis_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", analysis_date):
        raise ValueError("Demo analysis_date must be YYYY-MM-DD.")
    try:
        day = date.fromisoformat(analysis_date)
        day - timedelta(days=120)
    except (ValueError, OverflowError):
        raise ValueError("Demo analysis_date is invalid or outside the supported date range.") from None
    last = day - timedelta(days=1)
    ranges = {
        "last_30d": (day - timedelta(days=30), last),
        "current_7d": (day - timedelta(days=7), last),
        "previous_7d": (day - timedelta(days=14), day - timedelta(days=8)),
    }
    datasets, extras = [], {}
    for multiplier in range(1, 5):
        store = f"DemoStore{multiplier}"
        windows, raw_windows = {}, {}
        for name, (start, end) in ranges.items():
            rows = _window_rows(day, start, end, multiplier)
            windows[name] = {
                "start_date": str(start), "end_date": str(end), "complete": True,
                "zero_row_omission_confirmed": False, "captured_at": _stamp(day, 9, 59),
                "rows": rows,
            }
            raw_windows[name] = _raw_rows(store, rows)
        datasets.append({
            "store": store, "timezone": "Asia/Seoul", "currency": "KRW",
            "analysis_date": analysis_date, "observed_at": _stamp(day, 10),
            "data_kind": "synthetic_demo", "sales_basis": "platform_reported",
            "sales_basis_note": _BASIS_NOTE, "sales_windows": windows,
            "raw_windows": raw_windows,
        })
        extras[store] = _store_extras(store, day, multiplier)
    return {"datasets": datasets, "extras": extras}
