"""Offline WING collector checks using synthetic credentials and responses only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wing_client", ROOT / "skills" / "coupang-ops" / "scripts" / "wing_client.py"
)
wing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wing)

CONFIG = {
    "store": "SYNTHETIC_A", "cookie": "sc_vid=SYNTHETIC_A; XSRF-TOKEN=synthetic%2Btoken",
    "xsrf_token": "synthetic+token", "user_agent": "Synthetic offline test",
}


def item(sku: int, **metrics: object) -> dict:
    return {
        "vendorItemDetails": {
            "vendorId": "SYNTHETIC_A", "vendorItemId": sku,
            "itemName": f"Synthetic item {sku}", "productName": "Synthetic product",
            "registrationType": "NORMAL", "isOOS": False, "inventoryId": None,
        },
        "businessInsightsMetricsResponse": {
            "totalUnitsSold": sku, "totalGmv": "123.4500", "totalOrders": 1,
            "totalUniqueVisitor": 9, "totalPageViews": 12, "srpClick": 4,
            "searchVolume": 100, "pvToOrder": "0.01234567890123456789", **metrics,
        },
    }


def response_payload(page: int = 0, total: int = 21) -> dict:
    return {
        "vendorItems": [item(index + 1) for index in range(page * 20, min(total, (page + 1) * 20))],
        "soldVICount": total,
        "paginationDetails": {
            "pageSize": 20, "pageNumber": page, "totalResults": total,
            "totalPages": (total + 19) // 20,
        },
    }


class Response:
    def __init__(self, payload=None, *, status=200, body=None, headers=None, url=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = body if body is not None else json.dumps(payload).encode("utf-8")
        self.url = url or wing.API_URL
        self.closed = False

    def read(self, amount=-1):
        return self.body[:amount] if amount >= 0 else self.body

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, callback=None, total=21):
        self.calls = []
        self.responses = []
        self.callback = callback
        self.total = total

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        self.calls.append((request, payload, timeout))
        result = self.callback(payload, len(self.calls)) if self.callback else Response(
            response_payload(payload["pageNumber"], self.total)
        )
        self.responses.append(result)
        return result


class WingClientTests(unittest.TestCase):
    def collect(self, transport, config=None, **kwargs):
        return wing.fetch_store(config or CONFIG, "2026-10-12", transport,
                               request_interval=0, **kwargs)

    def test_timeout_retries_same_page_with_limit_but_unclassified_errors_stop(self):
        def intermittent(payload, call):
            if call == 2:
                raise URLError(TimeoutError("synthetic+token must not escape"))
            return Response(response_payload(payload["pageNumber"]))
        transport = Transport(intermittent)
        with patch.object(wing.time, "sleep"):
            data = self.collect(transport)
        self.assertEqual(len(data["sales_windows"]["last_30d"]["rows"]), 21)
        self.assertEqual([entry[1]["pageNumber"] for entry in transport.calls[:3]], [0, 1, 1])
        failing = Transport(lambda *_: (_ for _ in ()).throw(TimeoutError("synthetic+token")))
        with patch.object(wing.time, "sleep"), self.assertRaises(wing.NetworkError) as caught:
            self.collect(failing)
        self.assertEqual(len(failing.calls), 3)
        self.assertNotIn("synthetic+token", str(caught.exception))
        unclassified = Transport(lambda *_: (_ for _ in ()).throw(URLError("synthetic+token")))
        with self.assertRaises(wing.NetworkError):
            self.collect(unclassified)
        self.assertEqual(len(unclassified.calls), 1)

    def test_complete_paginated_windows_and_fixed_read_only_request(self):
        transport = Transport()
        data = self.collect(transport)
        self.assertEqual(data["store"], "SYNTHETIC_A")
        self.assertEqual(data["currency"], "KRW")
        self.assertEqual(data["timezone"], "Asia/Seoul")
        self.assertEqual(data["sales_basis"], "platform_reported")
        self.assertTrue(data["observed_at"].endswith("+09:00"))
        self.assertEqual(len(transport.calls), 6)
        expected = {
            "last_30d": ("2026-09-12", "2026-10-11"),
            "current_7d": ("2026-10-05", "2026-10-11"),
            "previous_7d": ("2026-09-28", "2026-10-04"),
        }
        for name, (start, end) in expected.items():
            window = data["sales_windows"][name]
            self.assertTrue(window["complete"])
            self.assertEqual((window["start_date"], window["end_date"]), (start, end))
            self.assertEqual(len(window["rows"]), 21)
            self.assertEqual(window["rows"][0]["amount"], "123.4500")
            self.assertEqual(window["rows"][0]["traffic"]["pvToOrder"], "0.01234567890123456789")
            self.assertEqual(len(data["raw_windows"][name]), 21)
        for request, body, timeout in transport.calls:
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, wing.API_URL)
            self.assertEqual(body["registrationTypes"], ["NORMAL", "RFM"])
            self.assertTrue(body["includeSoldVICount"])
            self.assertEqual((body["sortBy"], body["sortOrder"]), ("GMV", "DESC"))
            self.assertEqual(body["pageSize"], 20)
            self.assertEqual(request.get_header("Origin"), wing.ORIGIN)
            self.assertEqual(request.get_header("Referer"), wing.REFERER)
            self.assertEqual(request.get_header("X-xsrf-token"), "synthetic+token")
            self.assertEqual(timeout, 30)
        self.assertTrue(all(response.closed for response in transport.responses))
        serialized = json.dumps(data)
        self.assertNotIn(CONFIG["cookie"], serialized)
        self.assertNotIn(CONFIG["xsrf_token"], serialized)

    def test_numeric_json_decimal_preserves_precision(self):
        body = json.dumps(response_payload(total=1)).replace(
            '"123.4500"', "123.450000000000000000001"
        ).replace('"0.01234567890123456789"', "0.01234567890123456789")
        data = self.collect(Transport(lambda *_: Response(body=body.encode("utf-8"))))
        row = data["sales_windows"]["current_7d"]["rows"][0]
        self.assertEqual(row["amount"], "123.450000000000000000001")
        self.assertEqual(row["traffic"]["pvToOrder"], "0.01234567890123456789")

    def test_integral_json_decimal_counts_are_normalized_without_truncation(self):
        payload = response_payload(total=20)
        for row in payload["vendorItems"]:
            metrics = row["businessInsightsMetricsResponse"]
            for key in ("totalUnitsSold", "totalOrders", "totalUniqueVisitor", "totalPageViews", "srpClick", "searchVolume"):
                metrics[key] = float(metrics[key])
        # Real API JSON number tokens may be 20.0; the decoder preserves those
        # as Decimal, and only exact whole values may become normalized ints.
        data = self.collect(Transport(lambda *_: Response(payload)))
        row = data["sales_windows"]["current_7d"]["rows"][19]
        self.assertIs(type(row["units"]), int)
        self.assertEqual(row["units"], 20)
        self.assertEqual(row["traffic"]["totalPageViews"], 12)
        for value in ("20.5", True, "NaN", "-1000000000000000001.0", "1000000000000000001.0"):
            invalid = response_payload(total=1)
            invalid["vendorItems"][0]["businessInsightsMetricsResponse"]["totalUnitsSold"] = value
            with self.subTest(value=value), self.assertRaises(wing.SchemaError):
                self.collect(Transport(lambda *_: Response(invalid)))

    def test_optional_traffic_remains_unknown_but_required_sales_cannot_be_null(self):
        payload = response_payload(total=1)
        metrics = payload["vendorItems"][0]["businessInsightsMetricsResponse"]
        del metrics["totalUniqueVisitor"]
        metrics["totalOrders"] = None
        metrics["pvToOrder"] = None
        data = self.collect(Transport(lambda *_: Response(payload)))
        traffic = data["sales_windows"]["current_7d"]["rows"][0]["traffic"]
        self.assertIsNone(traffic["totalOrders"])
        self.assertIsNone(traffic["totalUniqueVisitor"])
        self.assertIsNone(traffic["pvToOrder"])
        metrics["totalUnitsSold"] = None
        with self.assertRaises(wing.SchemaError):
            self.collect(Transport(lambda *_: Response(payload)))

    def test_mixed_store_missing_fields_duplicate_sku_and_invalid_numbers(self):
        for problem in ("store", "missing", "duplicate", "NaN", "Infinity", "bool", "fraction_count"):
            with self.subTest(problem=problem):
                payload = response_payload(total=2)
                if problem == "store":
                    payload["vendorItems"][1]["vendorItemDetails"]["vendorId"] = "SYNTHETIC_B"
                elif problem == "missing":
                    del payload["vendorItems"][0]["businessInsightsMetricsResponse"]["totalGmv"]
                elif problem == "duplicate":
                    payload["vendorItems"][1]["vendorItemDetails"]["vendorItemId"] = 1
                else:
                    field = "totalUnitsSold" if problem in ("bool", "fraction_count") else "totalGmv"
                    value = {"NaN": "NaN", "Infinity": "Infinity",
                             "bool": True, "fraction_count": "1.5"}[problem]
                    payload["vendorItems"][0]["businessInsightsMetricsResponse"][field] = value
                with self.assertRaises(wing.SchemaError):
                    self.collect(Transport(lambda *_: Response(payload)))

    def test_signed_platform_sales_orders_and_gmv_are_preserved_but_visits_stay_nonnegative(self):
        payload = response_payload(total=1)
        metrics = payload["vendorItems"][0]["businessInsightsMetricsResponse"]
        metrics.update(totalUnitsSold="-1.0", totalOrders="-1.0", totalGmv="-16990.0", pvToOrder="-0.25")
        data = self.collect(Transport(lambda *_: Response(payload)))
        row = data["sales_windows"]["last_30d"]["rows"][0]
        self.assertEqual((row["units"], row["amount"], row["traffic"]["totalOrders"]), (-1, "-16990.0", -1))
        self.assertEqual(row["traffic"]["pvToOrder"], "-0.25")
        metrics["totalUniqueVisitor"] = "-1.0"
        with self.assertRaises(wing.SchemaError):
            self.collect(Transport(lambda *_: Response(payload)))

    def test_incomplete_or_changed_pagination_fails_without_partial_return(self):
        for problem in ("missing_row", "wrong_page", "changed_total", "wrong_size", "wrong_total_pages", "missing_pagination"):
            with self.subTest(problem=problem):
                def callback(request, call):
                    payload = response_payload(request["pageNumber"])
                    if call == 2:
                        if problem == "missing_row":
                            payload["vendorItems"] = []
                        elif problem == "wrong_page":
                            payload["paginationDetails"]["pageNumber"] = 0
                        elif problem == "changed_total":
                            payload["paginationDetails"]["totalResults"] = 22
                        elif problem == "wrong_size":
                            payload["paginationDetails"]["pageSize"] = 1
                        elif problem == "wrong_total_pages":
                            payload["paginationDetails"]["totalPages"] = 3
                        else:
                            del payload["paginationDetails"]
                    return Response(payload)
                transport = Transport(callback)
                with self.assertRaises(wing.SchemaError):
                    self.collect(transport)
                self.assertEqual(len(transport.calls), 2)

    def test_auth_redirect_html_http_errors_and_rate_limit_stop_immediately(self):
        cases = [
            (Response(status=302, headers={"Location": "https://login.invalid/?secret=synthetic+token"}), wing.AuthError),
            (Response(status=200, url="https://login.invalid/"), wing.AuthError),
            (Response(status=401), wing.AuthError), (Response(status=403), wing.AuthError),
            (Response(status=429), wing.RateLimitError), (Response(status=500), wing.NetworkError),
            (Response(body=b"<html>synthetic+token login</html>"), wing.AuthError),
            (Response(body=b"synthetic+token", headers={"Content-Type": "text/html"}), wing.AuthError),
            (Response({"code": "UNAUTHORIZED", "message": "synthetic+token"}), wing.AuthError),
            (Response({"loginUrl": "/login?token=synthetic+token"}), wing.AuthError),
        ]
        for response, error_class in cases:
            with self.subTest(status=response.status, error=error_class):
                transport = Transport(lambda *_: response)
                with self.assertRaises(error_class) as caught:
                    self.collect(transport)
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(response.closed)
                self.assertNotIn("synthetic+token", str(caught.exception))
                self.assertNotIn("Cookie", str(caught.exception))
                if response.status != 200:
                    self.assertIn(str(response.status), str(caught.exception))

    def test_progress_identifies_validated_page_without_authentication_values(self):
        events = []
        self.collect(Transport(), progress=events.append)
        self.assertEqual(events, [
            {"store": "SYNTHETIC_A", "window": window, "page": page, "total_pages": 2}
            for window in ("last_30d", "current_7d", "previous_7d") for page in (0, 1)
        ])
        self.assertNotIn(CONFIG["cookie"], json.dumps(events))
        self.assertNotIn(CONFIG["xsrf_token"], json.dumps(events))

    def test_invalid_count_names_the_static_field_and_progress_locates_the_page(self):
        for field in ("totalUnitsSold", "totalUniqueVisitor", "pageNumber", "soldVICount"):
            events = []
            def callback(request, call):
                payload = response_payload(request["pageNumber"])
                if call == 2:
                    if field == "pageNumber":
                        payload["paginationDetails"][field] = "synthetic+token"
                    elif field == "soldVICount":
                        payload[field] = "synthetic+token"
                    else:
                        payload["vendorItems"][0]["businessInsightsMetricsResponse"][field] = "synthetic+token"
                return Response(payload)
            with self.subTest(field=field), self.assertRaises(wing.SchemaError) as caught:
                self.collect(Transport(callback), progress=events.append)
            self.assertIn(field, str(caught.exception))
            self.assertNotIn("synthetic+token", str(caught.exception))
            self.assertEqual(events[-1]["page"], 0 if field in ("pageNumber", "soldVICount") else 1)

    def test_progress_callback_failure_is_sanitized(self):
        def progress(_):
            raise RuntimeError("synthetic+token Cookie")
        with self.assertRaises(wing.SchemaError) as caught:
            self.collect(Transport(), progress=progress)
        self.assertNotIn("synthetic+token", str(caught.exception))
        self.assertNotIn("Cookie", str(caught.exception))
        with self.assertRaises(wing.SchemaError):
            self.collect(Transport(), progress="synthetic+token")

    def test_network_exception_and_http_error_do_not_expose_secrets(self):
        for error in (
            URLError("Cookie sc_vid=SYNTHETIC_A; synthetic+token"),
            HTTPError(wing.API_URL, 403, "synthetic+token Cookie", {}, None),
            RuntimeError("synthetic+token Cookie"),
        ):
            with self.subTest(error=type(error).__name__):
                def transport(*_):
                    raise error
                with self.assertRaises(wing.WingError) as caught:
                    self.collect(transport)
                self.assertNotIn(CONFIG["xsrf_token"], str(caught.exception))
                self.assertNotIn("Cookie", str(caught.exception))
                formatted = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                              caught.exception.__traceback__))
                self.assertNotIn("synthetic+token Cookie", formatted)

    def test_currency_xsrf_boundaries_and_page_limit(self):
        for field, value, error_class in (
            ("currency", "USD", wing.SchemaError),
            ("xsrf_token", "synthetic token", wing.AuthError),
            ("cookie", "sc_vid=SYNTHETIC_B; XSRF-TOKEN=synthetic%2Btoken", wing.AuthError),
            ("user_agent", "fake\r\nSecret: injection", wing.AuthError),
        ):
            with self.subTest(field=field):
                config = dict(CONFIG, **{field: value})
                transport = Transport()
                with self.assertRaises(error_class):
                    self.collect(transport, config)
                self.assertEqual(transport.calls, [])
        with self.assertRaises(wing.SchemaError):
            self.collect(Transport(), max_pages=1)
        for invalid in (0, True, 1001):
            with self.assertRaises(wing.SchemaError):
                self.collect(Transport(), max_pages=invalid)
        data = self.collect(Transport(total=0))
        self.assertTrue(all(value["rows"] == [] for value in data["sales_windows"].values()))

    def test_duplicate_json_keys_and_nonstandard_json_numbers_are_rejected(self):
        for body in (b'{"vendorItems": [], "vendorItems": []}', b'{"totalGmv": NaN}',
                     b'{"totalGmv": Infinity}', b'{"vendorItems": []} trailing'):
            with self.subTest(body=body):
                with self.assertRaises(wing.SchemaError):
                    self.collect(Transport(lambda *_: Response(body=body)))

    def write_capture(self, text):
        directory = tempfile.TemporaryDirectory(prefix="wing-synthetic-test-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "capture.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_four_request_groups_multiline_cookie_and_deduplication(self):
        groups = []
        for index, store in enumerate(("SYNTHETIC_A", "SYNTHETIC_B", "SYNTHETIC_C", "SYNTHETIC_D", "SYNTHETIC_A")):
            colon = ":" if index % 2 == 0 else ""
            groups.append(
                f"Request URL:\n{wing.API_URL}\nRequest Headers\n"
                f"cookie{colon}\nsc_vid={store}; XSRF-TOKEN=fake%2B{index}\n"
                f"x-xsrf-token{colon}\nfake+{index}\nuser-agent:\nSynthetic user agent\n"
            )
        configs = wing.read_capture(self.write_capture("\n".join(groups)))
        self.assertEqual([config["store"] for config in configs],
                         ["SYNTHETIC_A", "SYNTHETIC_B", "SYNTHETIC_C", "SYNTHETIC_D"])
        self.assertEqual(configs[0]["xsrf_token"], "fake+0")

    def test_capture_inline_headers_and_json_response_allowlist(self):
        payload = response_payload(total=1)
        payload["Cookie"] = CONFIG["cookie"]
        payload["vendorItems"][0]["cookie"] = CONFIG["cookie"]
        payload["vendorItems"][0]["vendorItemDetails"]["xsrf_token"] = CONFIG["xsrf_token"]
        capture = (
            f"Request URL: {wing.API_URL}\nCookie: {CONFIG['cookie']}\n"
            f"X-XSRF-TOKEN: {CONFIG['xsrf_token']}\n"
            'Request Payload\n{"startDate":"2026-10-05"}\nResponse\n'
            + json.dumps(payload)
        )
        path = self.write_capture(capture)
        configs = wing.read_capture(path)
        self.assertEqual(configs[0]["store"], "SYNTHETIC_A")
        extracted = wing.extract_capture_response(path)
        self.assertEqual(extracted["paginationDetails"]["totalResults"], 1)
        serialized = json.dumps(extracted)
        self.assertNotIn(CONFIG["cookie"], serialized)
        self.assertNotIn(CONFIG["xsrf_token"], serialized)
        self.assertNotIn("cookie", serialized)

    def test_capture_never_reuses_credentials_from_another_request(self):
        first = (f"Request URL: {wing.API_URL}\nCookie: {CONFIG['cookie']}\n"
                 f"X-XSRF-TOKEN: {CONFIG['xsrf_token']}\n")
        second = f"Request URL: {wing.API_URL}\nCookie: sc_vid=SYNTHETIC_B; XSRF-TOKEN=fake\n"
        with self.assertRaises(wing.AuthError):
            wing.read_capture(self.write_capture(first + second))
        for destination in ("https://evil.invalid/", wing.API_URL + "?token=synthetic+token"):
            capture = first.replace(wing.API_URL, destination)
            with self.assertRaises(wing.SchemaError) as caught:
                wing.read_capture(self.write_capture(capture))
            self.assertNotIn("synthetic+token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
