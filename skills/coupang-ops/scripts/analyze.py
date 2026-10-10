"""Read-only normalized Coupang CSV analysis; Python standard library only."""

import argparse
import csv
import html
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


SALES_COLUMNS = {
    "store", "date", "timezone", "currency", "sku", "product_name",
    "ordered_units", "cancelled_units", "returned_units", "gross_revenue",
    "cancelled_revenue", "refunded_revenue",
}
INVENTORY_COLUMNS = {"store", "sku", "snapshot_at", "available_units", "lead_time_days"}
COUNT_FIELDS = ("ordered_units", "cancelled_units", "returned_units")
MONEY_FIELDS = ("gross_revenue", "cancelled_revenue", "refunded_revenue")
METRIC_FIELDS = COUNT_FIELDS + ("fulfilled_units", "net_units") + MONEY_FIELDS + ("net_revenue",)


def read_csv(path, columns):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{path.name}: 表头缺失或重复")
        missing = columns - set(reader.fieldnames)
        if missing:
            raise ValueError(f"{path.name}: 缺少列 {', '.join(sorted(missing))}")
        rows = []
        for line, row in enumerate(reader, 2):
            if None in row or any(row.get(key) is None or not row[key].strip() for key in columns):
                raise ValueError(f"{path.name}:{line}: 必填值缺失或列数不匹配")
            rows.append({key: value.strip() for key, value in row.items()})
        if not rows:
            raise ValueError(f"{path.name}: 没有数据")
        return rows


def count(value, label):
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{label}: 必须是非负整数")
    return int(value)


def money(value, label):
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label}: 金额格式错误") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label}: 金额必须是有限非负数")
    return result


def dates(start, end):
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def load_metadata(path):
    meta = json.loads(path.read_text(encoding="utf-8-sig"))
    required = {
        "data_kind", "store", "timezone", "currency", "sales_date_basis", "amount_basis",
        "return_attribution", "status_as_of", "previous_start", "previous_end",
        "current_start", "current_end", "stock_buffer_days",
    }
    if not isinstance(meta, dict) or required - set(meta):
        raise ValueError("元数据缺少必要的口径或期间字段，请参照 csv-schema.md")
    for key in required - {"stock_buffer_days"}:
        if not isinstance(meta[key], str) or not meta[key].strip():
            raise ValueError(f"元数据 {key}: 必须是非空字符串")
    supported = {
        "sales_date_basis": "order_date_local",
        "amount_basis": "gross_before_cancellations_and_refunds",
        "return_attribution": "original_order_date",
    }
    for key, expected in supported.items():
        if meta[key] != expected:
            raise ValueError(f"不支持的 {key}，需要 {expected}；请先确认并映射源数据")
    if meta["data_kind"] not in {"synthetic_demo", "user_export"}:
        raise ValueError("data_kind 必须为 synthetic_demo 或 user_export")
    if type(meta["stock_buffer_days"]) is not int or meta["stock_buffer_days"] < 0:
        raise ValueError("stock_buffer_days 必须是非负整数")
    periods = {}
    for name in ("previous", "current"):
        start = date.fromisoformat(meta[f"{name}_start"])
        end = date.fromisoformat(meta[f"{name}_end"])
        if start > end:
            raise ValueError("期间起始日期晚于结束日期")
        periods[name] = {"start": start, "end": end, "days": (end - start).days + 1}
    if periods["previous"]["end"] >= periods["current"]["start"]:
        raise ValueError("对比期间重叠或顺序错误")
    if periods["previous"]["days"] != periods["current"]["days"]:
        raise ValueError("对比期间长度必须一致")
    cutoff = datetime.fromisoformat(meta["status_as_of"])
    if cutoff.utcoffset() is None:
        raise ValueError("status_as_of 必须带 UTC 偏移量")
    expected_cutoff = datetime.combine(
        periods["current"]["end"] + timedelta(days=1), datetime.min.time(), cutoff.tzinfo,
    )
    if cutoff != expected_cutoff:
        raise ValueError("首版要求状态截止时间为本期结束后次日零点；请勿混入未完整期间")
    if cutoff > datetime.now(cutoff.tzinfo):
        raise ValueError("状态截止时间仍在未来，不能作为完整期间")
    return meta, periods, cutoff


def empty_metrics():
    return {field: Decimal(0) if field in MONEY_FIELDS + ("net_revenue",) else 0 for field in METRIC_FIELDS}


def growth(current, previous):
    return (Decimal(current) - Decimal(previous)) * 100 / Decimal(previous) if previous else None


def analyze(sales_path, metadata_path, inventory_path=None):
    meta, periods, cutoff = load_metadata(metadata_path)
    rows = read_csv(sales_path, SALES_COLUMNS)
    period_dates = {name: dates(value["start"], value["end"]) for name, value in periods.items()}
    all_dates = set().union(*period_dates.values())
    seen, products = set(), {}
    for index, row in enumerate(rows, 2):
        label = f"{sales_path.name}:{index}"
        for key in ("store", "timezone", "currency"):
            if row[key] != meta[key]:
                raise ValueError(f"{label}: {key} 与元数据不一致，不混合店铺/时区/币种")
        day = date.fromisoformat(row["date"])
        if day not in all_dates:
            raise ValueError(f"{label}: 日期不属于指定对比期间")
        sku = row["sku"]
        key = (row["store"], day, sku)
        if key in seen:
            raise ValueError(f"{label}: 重复键 {key}；未自动去重")
        seen.add(key)
        if sku not in products:
            products[sku] = {
                "sku": sku, "product_name": row["product_name"],
                "previous": empty_metrics(), "current": empty_metrics(), "inventory": None,
            }
        if row["product_name"] != products[sku]["product_name"]:
            raise ValueError(f"{label}: 同一 SKU 出现不同名称，请先确认映射")
        values = {field: count(row[field], f"{label} {field}") for field in COUNT_FIELDS}
        values.update({field: money(row[field], f"{label} {field}") for field in MONEY_FIELDS})
        values["fulfilled_units"] = values["ordered_units"] - values["cancelled_units"]
        values["net_units"] = values["fulfilled_units"] - values["returned_units"]
        values["net_revenue"] = values["gross_revenue"] - values["cancelled_revenue"] - values["refunded_revenue"]
        if values["net_units"] < 0 or values["net_revenue"] < 0:
            raise ValueError(f"{label}: 扣减超过下单件数或毛销售额，请确认是否重复扣除")
        if not values["ordered_units"] and any(values[field] for field in MONEY_FIELDS):
            raise ValueError(f"{label}: 零下单件数却存在金额，请确认口径")
        if not values["cancelled_units"] and values["cancelled_revenue"]:
            raise ValueError(f"{label}: 无取消件数却存在取消金额")
        period = "current" if day in period_dates["current"] else "previous"
        for field, value in values.items():
            products[sku][period][field] += value
    expected_rows = len(products) * len(all_dates)
    if len(seen) != expected_rows:
        missing = [
            f"{sku}/{day.isoformat()}" for sku in sorted(products) for day in sorted(all_dates)
            if (meta["store"], day, sku) not in seen
        ]
        raise ValueError(f"缺少 {len(missing)} 个 SKU/日期记录，例如 {', '.join(missing[:5])}；缺失不按零处理")
    if inventory_path:
        inventory_rows = read_csv(inventory_path, INVENTORY_COLUMNS)
        inventory_seen = set()
        for index, row in enumerate(inventory_rows, 2):
            label = f"{inventory_path.name}:{index}"
            sku = row["sku"]
            if row["store"] != meta["store"] or sku not in products:
                raise ValueError(f"{label}: 库存店铺或 SKU 与销售数据不匹配")
            if sku in inventory_seen:
                raise ValueError(f"{label}: 重复库存 SKU")
            inventory_seen.add(sku)
            snapshot = datetime.fromisoformat(row["snapshot_at"])
            if snapshot.utcoffset() != cutoff.utcoffset() or snapshot != cutoff:
                raise ValueError(f"{label}: 库存快照必须与状态截止时间一致（含 UTC 偏移）")
            available = count(row["available_units"], f"{label} available_units")
            lead = count(row["lead_time_days"], f"{label} lead_time_days")
            demand = Decimal(products[sku]["current"]["fulfilled_units"]) / periods["current"]["days"]
            coverage = Decimal(available) / demand if demand else None
            if coverage is None:
                risk = "本期无履约销量，无法估算"
            elif available == 0:
                risk = "当前无可售库存"
            elif coverage < lead:
                risk = "覆盖不足补货周期"
            elif coverage < lead + meta["stock_buffer_days"]:
                risk = "低于补货周期+缓冲"
            else:
                risk = "未触发当前阈值"
            products[sku]["inventory"] = {
                "available_units": available, "lead_time_days": lead,
                "snapshot_at": snapshot.isoformat(), "coverage_days": coverage, "risk": risk,
            }
    totals = {"previous": empty_metrics(), "current": empty_metrics()}
    for product in products.values():
        for period in totals:
            for field in METRIC_FIELDS:
                totals[period][field] += product[period][field]
        before, after = product["previous"], product["current"]
        product["change"] = {
            "net_units": after["net_units"] - before["net_units"],
            "net_units_pct": growth(after["net_units"], before["net_units"]),
            "net_revenue": after["net_revenue"] - before["net_revenue"],
            "net_revenue_pct": growth(after["net_revenue"], before["net_revenue"]),
        }
    return {
        "metadata": meta, "periods": periods, "totals": totals,
        "products": sorted(products.values(), key=lambda p: (-p["current"]["net_units"], p["sku"])),
        "checks": {"sales_rows": len(rows), "sku_count": len(products), "expected_rows": expected_rows},
    }


def text_safe(value):
    result = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    for character in ("\\", "|", "`", "[", "]", "*", "_"):
        result = result.replace(character, "\\" + character)
    return result


def amount(value):
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"


def pct(value, signed=False):
    if value is None:
        return "不可计算（上期为零）"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def return_rate(metrics):
    return Decimal(metrics["returned_units"]) * 100 / metrics["fulfilled_units"] if metrics["fulfilled_units"] else None


def render_report(result):
    meta, periods, totals, products = (result[key] for key in ("metadata", "periods", "totals", "products"))
    before, after = totals["previous"], totals["current"]
    current, previous = periods["current"], periods["previous"]
    sales_leader = products[0]
    revenue_leader = max(products, key=lambda p: p["current"]["net_revenue"])
    lines = [
        "# Coupang 运营分析：第一版试跑", "",
        "**模拟数据演示。店铺、商品、销量、金额和库存均为虚构，不能作为真实经营结论。**"
        if meta["data_kind"] == "synthetic_demo" else "依据用户提供的标准化导出数据计算。",
        "",
        f"店铺：{text_safe(meta['store'])}；时区：{text_safe(meta['timezone'])}；币种：{text_safe(meta['currency'])}。",
        f"本期：{current['start']} 至 {current['end']}；上期：{previous['start']} 至 {previous['end']}，均为 {current['days']} 个完整日历日。",
        f"订单状态观察截止：{text_safe(meta['status_as_of'])}。按订单原始日期归属取消和退货。", "",
        "## 本期概览", "",
        "| 指标 | 上期 | 本期 | 变化 |", "| --- | ---: | ---: | ---: |",
        f"| 净销量（件） | {before['net_units']} | {after['net_units']} | {pct(growth(after['net_units'], before['net_units']), True)} |",
        f"| 净销售额（{text_safe(meta['currency'])}） | {amount(before['net_revenue'])} | {amount(after['net_revenue'])} | {pct(growth(after['net_revenue'], before['net_revenue']), True)} |",
        f"| 取消件数 | {before['cancelled_units']} | {after['cancelled_units']} | {after['cancelled_units']-before['cancelled_units']:+d} 件 |",
        f"| 已观察到的退货件数 | {before['returned_units']} | {after['returned_units']} | {after['returned_units']-before['returned_units']:+d} 件 |", "",
        f"净销量第一：**{text_safe(sales_leader['product_name'])}**（{sales_leader['current']['net_units']} 件）。",
        f"净销售额第一：**{text_safe(revenue_leader['product_name'])}**（{amount(revenue_leader['current']['net_revenue'])} {text_safe(meta['currency'])}）。", "",
        "## 商品排名与变化", "",
        "按本期净销量降序。件数单位为件，净销售额单位为声明币种。", "",
        "| SKU / 商品 | 上期净销量 | 本期净销量 | 件数变化 | 销量变化率 | 本期净销售额 | 退货件数 / 履约件数 | 观察退货率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for product in products:
        old, now, change = product["previous"], product["current"], product["change"]
        rate = return_rate(now)
        rate_text = pct(rate) if rate is not None else "无履约销量"
        lines.append(
            f"| {text_safe(product['sku'])} / {text_safe(product['product_name'])} | {old['net_units']} | {now['net_units']} "
            f"| {change['net_units']:+d} | {pct(change['net_units_pct'], True)} | {amount(now['net_revenue'])} "
            f"| {now['returned_units']} / {now['fulfilled_units']} | {rate_text} |"
        )
    inventory_note = (
        f"快照：{text_safe(meta['status_as_of'])}。日均需求 = 本期履约件数 ÷ {current['days']}；覆盖天数 = 可售库存 ÷ 日均需求。"
        if any(product["inventory"] is not None for product in products)
        else "未提供库存快照，无法估算库存覆盖。"
    )
    lines += [
        "", "## 库存覆盖估计", "",
        inventory_note,
        f"退货未冲减需求；未假定退货可重新销售。缓冲假设为 {meta['stock_buffer_days']} 天，补货周期见下表。", "",
        "| 商品 | 可售库存（件） | 日均需求（件） | 覆盖天数 | 补货周期（天） | 提示 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for product in products:
        stock = product["inventory"]
        if stock is None:
            lines.append(f"| {text_safe(product['product_name'])} | 未提供 | — | 无法估算 | 未提供 | 库存未知 |")
            continue
        demand = Decimal(product["current"]["fulfilled_units"]) / current["days"]
        coverage = f"{stock['coverage_days']:.1f}" if stock["coverage_days"] is not None else "无法估算"
        lines.append(
            f"| {text_safe(product['product_name'])} | {stock['available_units']} | {demand:.1f} | {coverage} "
            f"| {stock['lead_time_days']} | {stock['risk']} |"
        )
    lines += ["", "## 建议关注", ""]
    for product in products:
        name, stock = text_safe(product["product_name"]), product["inventory"]
        if stock and stock["coverage_days"] is not None and stock["coverage_days"] < stock["lead_time_days"] + meta["stock_buffer_days"]:
            lines.append(f"- {name}：库存预计覆盖 {stock['coverage_days']:.1f} 天，核对在途数量和补货到货时间。")
        if product["change"]["net_units"] < 0:
            lines.append(f"- {name}：净销量从 {product['previous']['net_units']} 降至 {product['current']['net_units']} 件，核查商品状态、退货记录及其它经营数据。")
        if not product["previous"]["net_units"] and product["current"]["net_units"]:
            lines.append(f"- {name}：上期零销量，本期增加 {product['current']['net_units']} 件；不计算百分比增长，也不据此认定是新上架商品。")
    observed = [product for product in products if product["current"]["fulfilled_units"]]
    if observed:
        highest = max(observed, key=lambda p: return_rate(p["current"]))
        metrics = highest["current"]
        lines.append(f"- {text_safe(highest['product_name'])}：本期观察退货率在本组商品中最高，{metrics['returned_units']} / {metrics['fulfilled_units']} = {pct(return_rate(metrics))}，建议查看具体退货原因。")
    checks = result["checks"]
    lines += [
        "", "## 数据检查与口径", "",
        f"- 检查 {checks['sales_rows']} 条销售汇总、{checks['sku_count']} 个 SKU；两期每日每个 SKU 均有明确记录。零销量来自明确零值，不是对缺失记录补零。",
        "- 已检查必填值、重复店铺/日期/SKU、店铺/时区/币种一致性、有限非负金额和扣减范围。导出是否穷尽店铺全部商品仍取决于源数据声明。",
        "- 净销量 = 下单 − 取消 − 退货；履约量 = 下单 − 取消（输入仅包含已履约或取消订单）；净销售额 = 毛销售额 − 取消金额 − 退款金额。取消退款各扣除一次。",
        "- 观察退货率 = 同一订单日期批次的已观察退货件数 ÷ 履约件数；近期订单有更短的退货观察时间，退货率尚未成熟。",
        "- 未取得成本、费用、广告、流量或转化数据，因此不能计算利润或解释销量变化的原因。",
        "- 库存覆盖假设未来需求等于本期日均履约量，并不保证未来断货日期；未包含在途、季节变化或供货波动。",
        "- 计算脚本只读取本地输入并写入结果；未修改源文件或执行任何线上店铺操作。", "",
    ]
    return "\n".join(lines)


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description="Coupang 标准化 CSV 分析 / 模拟数据试跑")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--sales", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.demo:
        if args.sales or args.metadata or args.inventory:
            parser.error("--demo 不与自定义输入混用")
        demo = Path(__file__).resolve().parents[1] / "assets" / "demo"
        args.sales, args.metadata, args.inventory = (demo / name for name in ("sales.csv", "metadata.json", "inventory.csv"))
    elif not args.sales or not args.metadata:
        parser.error("请提供 --demo，或 --sales 与 --metadata")
    try:
        inputs = {path.resolve() for path in (args.sales, args.metadata, args.inventory) if path is not None}
        outputs = [(args.output_dir / name).resolve() for name in ("report.md", "summary.json")]
        if inputs.intersection(outputs):
            raise ValueError("输出路径会覆盖源文件，请使用单独的结果目录")
        result = analyze(args.sales, args.metadata, args.inventory)
        report = render_report(result)
        summary = json.dumps(result, ensure_ascii=False, indent=2, default=json_value) + "\n"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        outputs[0].write_text(report, encoding="utf-8")
        outputs[1].write_text(summary, encoding="utf-8")
        print("分析完成：" + str(outputs[0]))
        print("模拟数据" if args.demo else "用户声明的标准化数据")
        return 0
    except (ValueError, TypeError, OSError, InvalidOperation, OverflowError) as error:
        print(f"无法分析：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
