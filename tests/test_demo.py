"""Business-level CLI checks for the Coupang operations demonstration.

Run from the repository root with ``python -m unittest discover -s tests -v``.
Every CLI output and modified fixture is kept in a system temporary directory.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "coupang-ops" / "scripts" / "analyze.py"
DEMO = ROOT / "skills" / "coupang-ops" / "assets" / "demo"
SALES = DEMO / "sales.csv"
INVENTORY = DEMO / "inventory.csv"
METADATA = DEMO / "metadata.json"
SOURCE_FILES = (SALES, INVENTORY, METADATA)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CoupangDemoCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for path in (SCRIPT, *SOURCE_FILES):
            if not path.is_file():
                raise AssertionError(f"Required demonstration file is missing: {path}")
        with SALES.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            cls.sales_fields = list(reader.fieldnames or [])
            cls.sales_rows = list(reader)
        cls.metadata = json.loads(METADATA.read_text(encoding="utf-8-sig"))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="coupang-cli-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp = Path(self.temporary_directory.name)
        self.original_hashes = {path: sha256(path) for path in SOURCE_FILES}

    def tearDown(self) -> None:
        self.assertEqual(
            self.original_hashes,
            {path: sha256(path) for path in SOURCE_FILES},
            "Running an analysis must not modify the demonstration source files.",
        )

    def run_cli(
        self, arguments: list[str], output: Path, source_files: tuple[Path, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        before = {path: sha256(path) for path in source_files}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments, "--output-dir", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            before,
            {path: sha256(path) for path in source_files},
            "The CLI must leave its input files byte-for-byte unchanged.",
        )
        return result

    def successful_summary(
        self, arguments: list[str], name: str = "output", source_files: tuple[Path, ...] = ()
    ) -> dict:
        output = self.temp / name
        result = self.run_cli(arguments, output, source_files)
        self.assertEqual(
            result.returncode,
            0,
            f"CLI failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertTrue((output / "summary.json").is_file())
        self.assertTrue((output / "report.md").is_file())
        self.assertTrue((output / "report.md").read_text(encoding="utf-8-sig").strip())
        return json.loads((output / "summary.json").read_text(encoding="utf-8-sig"))

    def demo_summary(self) -> dict:
        return self.successful_summary(["--demo"], source_files=SOURCE_FILES)

    def write_fixture(self, name: str, rows: list[dict], metadata: dict) -> tuple[Path, Path]:
        fixture = self.temp / name
        fixture.mkdir()
        sales = fixture / "sales.csv"
        with sales.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=self.sales_fields)
            writer.writeheader()
            writer.writerows(rows)
        metadata_path = fixture / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return sales, metadata_path

    @staticmethod
    def products_by_sku(summary: dict) -> dict[str, dict]:
        return {product["sku"]: product for product in summary["products"]}

    def assert_decimal(self, actual: object, expected: str) -> None:
        self.assertIsInstance(actual, str, "Money and decimal values must be JSON strings.")
        self.assertEqual(Decimal(actual), Decimal(expected))

    def assert_percent(self, actual: object, expected: str) -> None:
        self.assertIsInstance(actual, str)
        self.assertEqual(Decimal(actual.rstrip("%")), Decimal(expected))

    def test_demo_totals_and_product_net_units(self) -> None:
        summary = self.demo_summary()
        for period, net_units, net_revenue in (
            ("previous", 224, "3131000"),
            ("current", 275, "3660500"),
        ):
            with self.subTest(period=period):
                totals = summary["totals"][period]
                self.assertEqual(totals["net_units"], net_units)
                self.assert_decimal(totals["net_revenue"], net_revenue)
                for field in ("ordered_units", "fulfilled_units", "net_units", "returned_units"):
                    self.assertIs(type(totals[field]), int)
                self.assertEqual(
                    totals["net_units"], totals["fulfilled_units"] - totals["returned_units"]
                )

        products = self.products_by_sku(summary)
        expected = {
            "SOCK-001": (90, 135),
            "TOWEL-002": (65, 84),
            "CUP-003": (50, 28),
            "BOX-004": (0, 28),
            "CASE-005": (19, 0),
        }
        self.assertEqual(set(products), set(expected))
        self.assertEqual(len(summary["products"]), len(expected), "SKU rows must not be duplicated.")
        for sku, (previous, current) in expected.items():
            with self.subTest(sku=sku):
                product = products[sku]
                self.assertTrue(product["product_name"])
                self.assertEqual(product["previous"]["net_units"], previous)
                self.assertEqual(product["current"]["net_units"], current)
                self.assertEqual(product["change"]["net_units"], current - previous)
                self.assert_decimal(
                    product["change"]["net_revenue"],
                    str(Decimal(product["current"]["net_revenue"])
                        - Decimal(product["previous"]["net_revenue"])),
                )

    def test_sales_volume_and_revenue_have_different_leading_products(self) -> None:
        products = self.demo_summary()["products"]
        volume_leader = max(products, key=lambda product: product["current"]["net_units"])
        revenue_leader = max(
            products, key=lambda product: Decimal(product["current"]["net_revenue"])
        )
        self.assertEqual(volume_leader["sku"], "SOCK-001")
        self.assertEqual(revenue_leader["sku"], "TOWEL-002")

    def test_growth_handles_zero_baseline_and_zero_current_demand(self) -> None:
        products = self.products_by_sku(self.demo_summary())
        self.assert_percent(products["SOCK-001"]["change"]["net_units_pct"], "50")
        new_product = products["BOX-004"]
        self.assertEqual(new_product["change"]["net_units"], 28)
        self.assertIsNone(new_product["change"]["net_units_pct"])
        self.assertIsNone(new_product["change"]["net_revenue_pct"])
        stopped_product = products["CASE-005"]
        self.assertEqual(stopped_product["current"]["net_units"], 0)
        self.assertEqual(stopped_product["current"]["fulfilled_units"], 0)
        self.assertEqual(stopped_product["change"]["net_units"], -19)
        self.assert_percent(stopped_product["change"]["net_units_pct"], "-100")
        self.assertIsNone(stopped_product["inventory"]["coverage_days"])

    def test_inventory_coverage_uses_fulfilled_units_over_seven_days(self) -> None:
        products = self.products_by_sku(self.demo_summary())
        for sku, available, fulfilled, expected_coverage in (
            ("SOCK-001", 30, 144, Decimal(30) / (Decimal(144) / 7)),
            ("BOX-004", 10, 28, Decimal("2.5")),
        ):
            with self.subTest(sku=sku):
                product = products[sku]
                inventory = product["inventory"]
                self.assertEqual(product["current"]["fulfilled_units"], fulfilled)
                self.assertEqual(inventory["available_units"], available)
                self.assertIs(type(inventory["lead_time_days"]), int)
                self.assertIsInstance(inventory["snapshot_at"], str)
                self.assertTrue(inventory["snapshot_at"])
                self.assertIsInstance(inventory["risk"], str)
                self.assertTrue(inventory["risk"])
                self.assertIsInstance(inventory["coverage_days"], str)
                self.assertAlmostEqual(
                    Decimal(inventory["coverage_days"]),
                    expected_coverage,
                    delta=Decimal("0.01"),
                )

    def test_cancellations_and_refunds_are_subtracted_exactly_once(self) -> None:
        summary = self.demo_summary()
        expected_units: dict[str, int] = {}
        expected_revenue: dict[str, Decimal] = {}
        cancelled_revenue = Decimal(0)
        refunded_revenue = Decimal(0)
        for row in self.sales_rows:
            sku = row["sku"]
            expected_units[sku] = expected_units.get(sku, 0) + (
                int(row["ordered_units"]) - int(row["cancelled_units"]) - int(row["returned_units"])
            )
            cancellation = Decimal(row["cancelled_revenue"])
            refund = Decimal(row["refunded_revenue"])
            expected_revenue[sku] = expected_revenue.get(sku, Decimal(0)) + (
                Decimal(row["gross_revenue"]) - cancellation - refund
            )
            cancelled_revenue += cancellation
            refunded_revenue += refund
        self.assertGreater(cancelled_revenue, 0, "The fixture must exercise cancellation deductions.")
        self.assertGreater(refunded_revenue, 0, "The fixture must exercise refund deductions.")
        for sku, product in self.products_by_sku(summary).items():
            with self.subTest(sku=sku):
                self.assertEqual(
                    product["previous"]["net_units"] + product["current"]["net_units"],
                    expected_units[sku],
                )
                self.assertEqual(
                    Decimal(product["previous"]["net_revenue"])
                    + Decimal(product["current"]["net_revenue"]),
                    expected_revenue[sku],
                )
        self.assertEqual(
            sum(Decimal(summary["totals"][period]["net_revenue"])
                for period in ("previous", "current")),
            sum(expected_revenue.values(), Decimal(0)),
        )

    def test_custom_sales_without_inventory_keeps_inventory_unknown(self) -> None:
        sales, metadata = self.write_fixture("custom", self.sales_rows, self.metadata)
        summary = self.successful_summary(
            ["--sales", str(sales), "--metadata", str(metadata)],
            source_files=(sales, metadata),
        )
        self.assertEqual(summary["totals"]["previous"]["net_units"], 224)
        self.assertEqual(summary["totals"]["current"]["net_units"], 275)
        self.assert_decimal(summary["totals"]["current"]["net_revenue"], "3660500")
        self.assertTrue(summary["products"])
        for product in summary["products"]:
            with self.subTest(sku=product["sku"]):
                self.assertIsNone(product["inventory"])

    def test_an_additional_refund_reduces_current_revenue_by_its_exact_amount(self) -> None:
        current_dates = set(sorted({row["date"] for row in self.sales_rows})[-7:])
        rows = copy.deepcopy(self.sales_rows)
        candidates = [
            row for row in rows
            if row["date"] in current_dates
            and Decimal(row["gross_revenue"]) - Decimal(row["cancelled_revenue"])
            - Decimal(row["refunded_revenue"]) >= Decimal("100")
        ]
        self.assertTrue(candidates, "The current period must contain a refundable sale.")
        changed = candidates[-1]
        changed_sku = changed["sku"]
        changed["refunded_revenue"] = str(Decimal(changed["refunded_revenue"]) + 100)
        sales, metadata = self.write_fixture("extra-refund", rows, self.metadata)
        summary = self.successful_summary(
            ["--sales", str(sales), "--metadata", str(metadata)],
            source_files=(sales, metadata),
        )
        baseline = self.successful_summary(["--demo"], name="baseline", source_files=SOURCE_FILES)
        self.assertEqual(summary["totals"]["previous"], baseline["totals"]["previous"])
        self.assertEqual(
            Decimal(summary["totals"]["current"]["net_revenue"]),
            Decimal(baseline["totals"]["current"]["net_revenue"]) - 100,
        )
        product = self.products_by_sku(summary)[changed_sku]
        original_product = self.products_by_sku(baseline)[changed_sku]
        self.assertEqual(
            Decimal(product["current"]["net_revenue"]),
            Decimal(original_product["current"]["net_revenue"]) - 100,
        )
        self.assertEqual(product["current"]["net_units"], original_product["current"]["net_units"])

    def test_invalid_business_data_is_rejected_before_report_creation(self) -> None:
        for case in ("incompatible_amount_basis", "duplicate_key", "missing_sku_day", "nan_amount", "mixed_currency"):
            with self.subTest(case=case):
                rows = copy.deepcopy(self.sales_rows)
                metadata = copy.deepcopy(self.metadata)
                if case == "incompatible_amount_basis":
                    metadata["amount_basis"] = "net_after_cancellations_and_refunds"
                elif case == "duplicate_key":
                    rows.append(copy.deepcopy(rows[0]))
                elif case == "missing_sku_day":
                    del rows[0]
                elif case == "nan_amount":
                    rows[0]["gross_revenue"] = "NaN"
                elif case == "mixed_currency":
                    rows[-1]["currency"] = "USD" if rows[0]["currency"] != "USD" else "KRW"
                sales, metadata_path = self.write_fixture(case, rows, metadata)
                output = self.temp / f"{case}-output"
                result = self.run_cli(
                    ["--sales", str(sales), "--metadata", str(metadata_path)],
                    output,
                    source_files=(sales, metadata_path),
                )
                self.assertNotEqual(result.returncode, 0, f"Invalid input was accepted: {case}")
                self.assertFalse((output / "report.md").exists(), "Rejected data must not produce a report.")
                self.assertTrue((result.stderr + result.stdout).strip(), "Validation should explain its failure.")

    def test_demo_source_files_remain_byte_for_byte_unchanged(self) -> None:
        self.demo_summary()
        self.assertEqual(self.original_hashes, {path: sha256(path) for path in SOURCE_FILES})


if __name__ == "__main__":
    unittest.main()
