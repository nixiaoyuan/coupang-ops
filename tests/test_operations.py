"""Business and completeness checks for the store-scoped operations API."""

from __future__ import annotations

import copy
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "coupang-ops" / "scripts" / "operations.py"
SPEC = importlib.util.spec_from_file_location("operations", SCRIPT)
operations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operations)


def row(sku, units, amount):
    return {"sku": sku, "name": "商品 " + sku, "units": units, "amount": amount,
            "traffic": {"totalUniqueVisitor": 100, "totalPageViews": 200,
                        "srpClick": 25, "searchVolume": 30, "pvToOrder": "0.07"}}


def fixture():
    data = {
        "store": "测试店", "timezone": "Asia/Seoul", "currency": "KRW",
        "analysis_date": "2026-10-12", "observed_at": "2026-10-12T10:00:00+09:00",
        "sales_basis": "platform_reported", "sales_basis_note": "平台汇总；取消和退款定义尚未确认",
        "sales_windows": {
            "last_30d": {"start_date": "2026-09-12", "end_date": "2026-10-11", "complete": True,
                         "rows": [row("A", 60, "600.10"), row("B", 20, "2000.20"), row("C", 0, "0"), row("D", 0, "0")]},
            "current_7d": {"start_date": "2026-10-05", "end_date": "2026-10-11", "complete": True,
                           "rows": [row("A", 14, "140"), row("B", 13, "1300"), row("C", 0, "0"), row("D", 0, "0")]},
            "previous_7d": {"start_date": "2026-09-28", "end_date": "2026-10-04", "complete": True,
                            "rows": [row("A", 20, "200"), row("B", 20, "2000"), row("C", 0, "0"), row("D", 30, "300")]},
        },
    }
    extras = {
        "store": "测试店",
        "coverage": {"products_complete": True, "inventory_complete": True,
                     "shipments_complete": True, "shipments_start_date": "2026-09-12", "shipments_end_date": "2026-10-11",
                     "returns_complete": True, "transactions_complete": True,
                     "transactions_start_date": "2026-09-12", "transactions_end_date": "2026-10-11"},
        "products": [{"sku": sku, "name": "商品 " + sku, "first_listed_at": "2026-07-01",
                      "last_deal_at": "2026-10-10" if sku != "C" else None, "unit_cost": None}
                     for sku in "ABCD"],
        "inventory": [{"sku": sku, "available_units": available, "snapshot_at": "2026-10-12T09:45:30+09:00"}
                      for sku, available in (("A", 20), ("B", 10), ("C", 5), ("D", 10))],
        "shipments": [
            {"shipment_line_id": "S-A", "order_id": "O-A", "sku": "A", "shipped_at": "2026-10-10T12:00:00+09:00", "shipped_units": 42},
            {"shipment_line_id": "S-B", "order_id": "O-B", "sku": "B", "shipped_at": "2026-09-20T12:00:00+09:00", "shipped_units": 28},
            {"shipment_line_id": "S-D", "order_id": "O-D", "sku": "D", "shipped_at": "2026-10-09T12:00:00+09:00", "shipped_units": 30},
            {"shipment_line_id": "S-old", "order_id": "O-old", "sku": "A", "shipped_at": "2026-09-01T12:00:00+09:00", "shipped_units": 50},
        ],
        "returns": [
            {"return_line_id": "R-A", "shipment_line_id": "S-A", "returned_units": 5, "completed_at": "2026-10-11T12:00:00+09:00", "reason": "尺寸不符"},
            {"return_line_id": "R-B-1", "shipment_line_id": "S-B", "returned_units": 2, "completed_at": "2026-10-11T12:00:00+09:00", "reason": "破损"},
            {"return_line_id": "R-B-2", "shipment_line_id": "S-B", "returned_units": 1, "completed_at": "2026-10-12T09:00:00+09:00", "reason": None},
            {"return_line_id": "R-D", "shipment_line_id": "S-D", "returned_units": 30, "completed_at": "2026-10-11T12:00:00+09:00", "reason": "退款"},
            {"return_line_id": "R-old", "shipment_line_id": "S-old", "returned_units": 20, "completed_at": "2026-10-11T12:00:00+09:00", "reason": "旧批次"},
            {"return_line_id": "R-later", "shipment_line_id": "S-A", "returned_units": 1, "completed_at": "2026-10-13T12:00:00+09:00", "reason": "截止后"},
        ],
        "transactions": [{"transaction_line_id": "T-" + sku, "sku": sku, "occurred_at": stamp, "units": units}
                         for sku, stamp, units in (("A", "2026-10-10T12:00:00+09:00", 42),
                                                   ("B", "2026-09-20T12:00:00+09:00", 28),
                                                   ("D", "2026-10-09T12:00:00+09:00", 30))],
    }
    return data, extras


def by_sku(section):
    return {row["sku"]: row for row in section["rows"]}


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.data, self.extras = fixture()

    def analyze(self, **kwargs):
        return operations.analyze_store(self.data, self.extras, **kwargs)

    def test_rankings_use_separate_metrics_and_one_full_store_denominator(self):
        result = self.analyze()["ranking"]
        self.assertEqual(result["top_units"][0]["sku"], "A")
        self.assertEqual(result["top_amount"][0]["sku"], "B")
        self.assertEqual(result["denominator"]["units"], 80)
        self.assertEqual(Decimal(result["denominator"]["amount"]), Decimal("2600.30"))
        self.assertEqual(Decimal(result["top_units"][0]["units_share"]), Decimal(".75"))
        self.assertEqual(result["amount_label"], "平台销售额 / GMV")
        self.assertNotIn("净", result["units_label"])

    def test_top_ten_shares_keep_all_store_denominator(self):
        self.data["sales_windows"]["last_30d"]["rows"] = [row(str(i), i + 1, str(i + 1)) for i in range(12)]
        result = self.analyze()["ranking"]
        self.assertEqual(len(result["top_units"]), 10)
        self.assertEqual(len(result["top_amount"]), 10)
        self.assertEqual(result["denominator"]["units"], 78)
        self.assertEqual(Decimal(result["top_units"][0]["units_share"]), Decimal(12) / 78)

    def test_confirmed_net_basis_changes_labels_without_deducting_again(self):
        self.data["sales_basis"] = "net_confirmed"
        self.data["sales_basis_note"] = "确认已扣取消退款"
        result = self.analyze()["ranking"]
        self.assertEqual(result["units_label"], "净销量")
        self.assertEqual(result["top_units"][0]["units"], 60)
        self.assertEqual(result["top_amount"][0]["amount"], "2000.20")

    def test_signed_platform_sales_preserve_adjustments_without_becoming_shipments(self):
        self.data["sales_windows"]["last_30d"]["rows"].append(row("E", -1, "-16990.0"))
        self.data["sales_windows"]["current_7d"]["rows"][0]["units"] = -1
        result = self.analyze()
        self.assertEqual(result["ranking"]["denominator"]["units"], 79)
        self.assertEqual(Decimal(result["ranking"]["denominator"]["amount"]), Decimal("-14389.7"))
        self.assertTrue(all(item["amount_share"] is None for item in result["ranking"]["rows"]))
        self.assertEqual(by_sku(result["sales_drop"])["A"]["drop_fraction"], "1.05")
        self.assertTrue(by_sku(result["sales_drop"])["A"]["alert"])
        self.assertEqual(by_sku(result["stock_risk"])["A"]["shipped_units_14d"], 42)

    def test_drop_threshold_is_strict_and_requires_twenty_baseline_units(self):
        rows = by_sku(self.analyze()["sales_drop"])
        self.assertFalse(rows["A"]["alert"], "exactly 30% is not greater than 30%")
        self.assertTrue(rows["B"]["alert"])
        self.assertIsNone(rows["C"]["drop_fraction"])
        self.data["sales_windows"]["previous_7d"]["rows"][1]["units"] = 19
        self.data["sales_windows"]["current_7d"]["rows"][1]["units"] = 0
        self.assertFalse(by_sku(self.analyze()["sales_drop"])["B"]["alert"])

    def test_missing_period_sku_is_unknown_until_zero_omission_confirmed(self):
        self.data["sales_windows"]["current_7d"]["rows"].pop(1)
        result = self.analyze()["sales_drop"]
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(by_sku(result)["B"]["current_units"])
        self.assertIsNone(by_sku(result)["B"]["alert"])
        self.data["sales_windows"]["current_7d"]["zero_row_omission_confirmed"] = True
        result = self.analyze()["sales_drop"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(by_sku(result)["B"]["current_units"], 0)
        self.assertTrue(by_sku(result)["B"]["alert"])

    def test_zero_omission_declaration_does_not_make_partial_period_complete(self):
        period = self.data["sales_windows"]["current_7d"]
        period["rows"].pop(1)
        period["complete"] = False
        period["zero_row_omission_confirmed"] = True
        result = self.analyze()["sales_drop"]
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(by_sku(result)["B"]["current_units"])
        self.assertIsNone(by_sku(result)["A"]["alert"])

    def test_sku_missing_in_both_periods_is_still_unknown_when_known_in_store(self):
        for name in ("current_7d", "previous_7d"):
            self.data["sales_windows"][name]["rows"] = [entry for entry in self.data["sales_windows"][name]["rows"] if entry["sku"] != "B"]
        section = self.analyze()["sales_drop"]
        self.assertEqual(section["status"], "incomplete")
        self.assertIsNone(by_sku(section)["B"]["previous_units"])
        self.assertIsNone(by_sku(section)["B"]["current_units"])
        for name in ("current_7d", "previous_7d"):
            self.data["sales_windows"][name]["zero_row_omission_confirmed"] = True
        section = self.analyze()["sales_drop"]
        self.assertEqual(section["status"], "ok")
        self.assertFalse(by_sku(section)["B"]["alert"])

    def test_partial_ranking_has_no_all_store_share_or_total(self):
        self.data["sales_windows"]["last_30d"]["complete"] = False
        result = self.analyze()["ranking"]
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["denominator"]["units"])
        self.assertIsNone(result["top_units"][0]["units_share"])
        self.assertEqual(result["denominator"]["observed_units"], 80)

    def test_missing_sources_keep_five_sections_without_healthy_zero_results(self):
        result = operations.analyze_store(self.data)
        for key in ("stock_risk", "slow_stock", "returns"):
            self.assertEqual(result[key]["status"], "missing_data")
            self.assertTrue(result[key]["missing"])
        self.assertEqual(result["ranking"]["status"], "ok")
        report = operations.render_report([result])
        self.assertIn("缺少必要数据", report)
        self.assertNotIn("库存正常", report)

    def test_stock_uses_actual_fourteen_day_shipments_and_real_snapshot(self):
        result = self.analyze()["stock_risk"]
        rows = by_sku(result)
        self.assertEqual(rows["A"]["shipped_units_14d"], 42)
        self.assertEqual(Decimal(rows["A"]["daily_shipped_units"]), Decimal(3))
        self.assertEqual(Decimal(rows["A"]["coverage_days"]), Decimal(20) / 3)
        self.assertTrue(rows["A"]["alert"])
        self.assertEqual(rows["A"]["snapshot_at"], "2026-10-12T09:45:30+09:00")
        self.assertIsNone(rows["B"]["coverage_days"], "old shipments are not current demand")
        self.assertIsNone(rows["B"]["alert"])
        self.assertEqual(rows["B"]["assessment"], "unknown_demand")

    def test_seven_day_stock_boundary_is_not_alert(self):
        self.extras["inventory"][0]["available_units"] = 21
        result = by_sku(self.analyze()["stock_risk"])["A"]
        self.assertEqual(Decimal(result["coverage_days"]), Decimal(7))
        self.assertFalse(result["alert"])

    def test_short_shipment_coverage_cannot_estimate_demand(self):
        self.extras["coverage"]["shipments_start_date"] = "2026-10-01"
        section = self.analyze()["stock_risk"]
        self.assertEqual(section["status"], "incomplete")
        self.assertIsNone(by_sku(section)["A"]["coverage_days"])
        self.assertEqual(by_sku(section)["A"]["observed_shipped_units_14d"], 42)

    def test_missing_inventory_sku_is_unknown_even_with_global_completeness_claim(self):
        self.extras["inventory"].pop(0)
        summary = self.analyze()
        section = summary["stock_risk"]
        self.assertEqual(section["status"], "incomplete")
        self.assertIsNone(by_sku(section)["A"]["available_units"])
        self.assertIsNone(by_sku(section)["A"]["alert"])
        self.assertEqual(summary["slow_stock"]["status"], "incomplete")
        self.assertIn("库存快照缺失，无法估算", operations.render_report([summary]))

    def test_net_zero_from_full_return_is_not_zero_transactions(self):
        self.data["sales_basis"] = "net_confirmed"
        summary = self.analyze()
        rows = by_sku(summary["slow_stock"])
        self.assertEqual(by_sku(summary["ranking"])["D"]["units"], 0)
        self.assertEqual(rows["D"]["non_cancelled_transaction_units_30d"], 30)
        self.assertFalse(rows["D"]["alert"])
        self.assertTrue(rows["C"]["alert"])
        self.assertIsNone(rows["C"]["last_deal_at"])
        self.assertEqual(rows["C"]["last_deal_status"], "unknown")
        report = operations.render_report([summary])
        self.assertNotIn("从未成交", report.replace("不表示从未成交", ""))

    def test_incomplete_transactions_cannot_confirm_zero_deals(self):
        self.extras["coverage"]["transactions_complete"] = False
        section = self.analyze()["slow_stock"]
        self.assertEqual(section["status"], "incomplete")
        self.assertIsNone(by_sku(section)["C"]["non_cancelled_transaction_units_30d"])
        self.assertIsNone(by_sku(section)["C"]["alert"])

    def test_recent_listing_does_not_trigger_slow_stock(self):
        self.extras["products"][2]["first_listed_at"] = "2026-09-20"
        result = by_sku(self.analyze()["slow_stock"])["C"]
        self.assertFalse(result["eligible"])
        self.assertFalse(result["alert"])

    def test_return_cohort_matches_shipment_not_contemporaneous_return_date(self):
        section = self.analyze()["returns"]
        rows = by_sku(section)
        self.assertEqual(section["status"], "provisional")
        self.assertEqual(rows["A"]["shipped_units"], 42)
        self.assertEqual(rows["A"]["returned_units"], 5)
        self.assertEqual(section["ignored_outside_cohort"], 1)
        self.assertEqual(section["ignored_after_cutoff"], 1)
        self.assertEqual(rows["B"]["returned_units"], 3)
        self.assertEqual(rows["B"]["reason_distribution_units"], {"破损": 2})
        self.assertEqual(rows["B"]["unknown_reason_units"], 1)
        self.assertEqual(rows["B"]["unknown_reason_count"], 1)

    def test_return_threshold_is_strict_and_minimum_is_required(self):
        self.extras["returns"][0]["returned_units"] = 4
        self.extras["shipments"][0]["shipped_units"] = 40
        result = by_sku(self.analyze()["returns"])["A"]
        self.assertEqual(Decimal(result["return_fraction"]), Decimal(".1"))
        self.assertFalse(result["alert"])
        self.extras["shipments"][0]["shipped_units"] = 19
        self.assertFalse(by_sku(self.analyze()["returns"])["A"]["alert"])

    def test_partial_return_source_does_not_claim_complete_ratio(self):
        self.extras["coverage"]["returns_complete"] = False
        section = self.analyze()["returns"]
        self.assertEqual(section["status"], "incomplete")
        self.assertIsNone(by_sku(section)["A"]["return_fraction"])
        self.assertIsNone(by_sku(section)["A"]["alert"])
        self.assertEqual(by_sku(section)["A"]["observed_returned_units"], 5)

    def test_unmatched_return_is_reported_as_incomplete(self):
        self.extras["returns"][0]["shipment_line_id"] = "not-supplied"
        section = self.analyze()["returns"]
        self.assertEqual(section["status"], "incomplete")
        self.assertTrue(any("matching_shipment_line" in missing for missing in section["missing"]))
        self.assertIsNone(by_sku(section)["A"]["alert"])

    def test_timestamps_compare_instants_and_windows_use_seoul_local_dates(self):
        self.extras["inventory"][0]["snapshot_at"] = "2026-10-12T00:45:30Z"
        self.extras["shipments"][0]["shipped_at"] = "2026-09-27T16:00:00Z"  # Sep 28 in Seoul
        self.extras["returns"][0]["completed_at"] = "2026-10-12T01:00:00Z"  # Exactly cutoff
        section = self.analyze()
        self.assertEqual(by_sku(section["stock_risk"])["A"]["shipped_units_14d"], 42)
        self.assertEqual(by_sku(section["returns"])["A"]["returned_units"], 5)

    def test_invalid_timestamps_and_future_snapshot_are_rejected(self):
        for stamp in ("2026-10-12", "2026-10-12T09:45:30", "2026-10-12T11:00:00+09:00"):
            with self.subTest(stamp=stamp):
                self.extras["inventory"][0]["snapshot_at"] = stamp
                with self.assertRaises(ValueError):
                    self.analyze()

    def test_wrong_window_and_pre_window_observation_are_rejected(self):
        self.data["sales_windows"]["current_7d"]["end_date"] = "2026-10-12"
        with self.assertRaises(ValueError):
            self.analyze()
        self.data, self.extras = fixture()
        self.data["observed_at"] = "2026-10-11T23:59:59+09:00"
        with self.assertRaises(ValueError):
            self.analyze()

    def test_duplicate_and_over_returned_lines_are_rejected(self):
        for case in ("duplicate_shipment", "duplicate_return", "over_return", "return_before_shipment"):
            with self.subTest(case=case):
                self.data, self.extras = fixture()
                if case == "duplicate_shipment":
                    self.extras["shipments"].append(copy.deepcopy(self.extras["shipments"][0]))
                elif case == "duplicate_return":
                    self.extras["returns"].append(copy.deepcopy(self.extras["returns"][0]))
                elif case == "over_return":
                    self.extras["returns"][0]["returned_units"] = 42  # plus another partial return
                else:
                    self.extras["returns"][0]["completed_at"] = "2026-10-01T00:00:00+09:00"
                with self.assertRaises(ValueError):
                    self.analyze()

    def test_invalid_numbers_cross_store_and_net_aliases_are_rejected(self):
        for case in ("nan_money", "negative_units", "bool_units", "mixed_store", "net_alias", "infinite_traffic", "bool_coverage"):
            with self.subTest(case=case):
                self.data, self.extras = fixture()
                if case == "nan_money":
                    self.data["sales_windows"]["last_30d"]["rows"][0]["amount"] = "NaN"
                elif case == "negative_units":
                    self.extras["shipments"][0]["shipped_units"] = -1
                elif case == "bool_units":
                    self.extras["transactions"][0]["units"] = True
                elif case == "mixed_store":
                    self.extras["inventory"][0]["store"] = "另一店"
                elif case == "net_alias":
                    self.extras["shipments"][0]["net_units"] = self.extras["shipments"][0].pop("shipped_units")
                elif case == "infinite_traffic":
                    self.data["sales_windows"]["current_7d"]["rows"][0]["traffic"]["pvToOrder"] = "Infinity"
                else:
                    self.extras["coverage"]["products_complete"] = "true"
                with self.assertRaises(ValueError):
                    self.analyze()

    def test_traffic_is_descriptive_and_does_not_create_causal_claims(self):
        self.data["sales_windows"]["current_7d"]["rows"][1]["traffic"]["totalPageViews"] = 1
        result = self.analyze()
        drop = by_sku(result["sales_drop"])["B"]
        self.assertTrue(drop["alert"])
        self.assertEqual(drop["current_traffic"]["totalPageViews"], "1")
        self.assertIn("无法据此确定", result["sales_drop"]["traffic_note"])
        self.assertNotIn("流量下降导致", operations.render_report([result]))

    def test_summary_is_json_compatible_and_inputs_remain_unchanged(self):
        original = copy.deepcopy((self.data, self.extras))
        result = self.analyze()
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertEqual(json.loads(encoded)["store"], "测试店")
        self.assertEqual((self.data, self.extras), original)

    def test_chinese_report_contains_five_answers_store_snapshot_cutoff_and_limits(self):
        result = self.analyze()
        report = operations.render_report([result])
        for fragment in ("测试店", "### 1.", "### 2.", "### 3.", "### 4.", "### 5.",
                         "2026-10-12T09:45:30+09:00", "2026-10-12T10:00:00+09:00", "未知成本", "尚未成熟"):
            self.assertIn(fragment, report)

    def test_unknown_threshold_and_nonfinite_threshold_are_rejected(self):
        for limits in ({"typo": 1}, {"stock_days": "NaN"}, {"drop_fraction": "1.01"}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.analyze(thresholds=limits)


if __name__ == "__main__":
    unittest.main()
