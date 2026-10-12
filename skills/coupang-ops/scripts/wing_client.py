"""Read-only WING business-insight collector using the Python standard library.

Capture credentials stay in memory and are forwarded only to the fixed WING
endpoint. Exceptions deliberately contain no request values, response bodies,
URLs, or filesystem paths. A custom transport can be injected for offline tests;
it receives ``(urllib.request.Request, timeout_seconds)`` and returns a response
with ``status``, ``headers``, ``read()``, and optionally ``geturl()`` / ``close()``.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_URL = "https://wing.coupang.com/tenants/rfm-ss/api/business-insight/vi-detail-search"
ORIGIN = "https://wing.coupang.com"
REFERER = "https://wing.coupang.com/tenants/business-insight/sales-analysis"
KST = timezone(timedelta(hours=9), "Asia/Seoul")
PAGE_SIZE = 20
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_CAPTURE_BYTES = 40 * 1024 * 1024
TIMEOUT_SECONDS = 30
DEFAULT_USER_AGENT = "Mozilla/5.0 CoupangOpsReadOnly/1.0"
SALES_BASIS_NOTE = (
    "使用 WING business-insight 后台原值；汇总销量、订单数和 GMV 可为负数，原样保留。"
    "取消、退款及日期归属的具体扣减口径尚未确认，不称为已核验的净指标，也不再次扣减。"
)
_DETAIL_FIELDS = (
    "vendorId", "vendorItemId", "itemName", "productName", "registrationType",
    "isOOS", "inventoryId",
)
_COUNT_METRICS = (
    "totalOrders", "totalUniqueVisitor", "totalPageViews", "srpClick", "searchVolume",
)
_METRIC_FIELDS = ("totalUnitsSold", "totalGmv", *_COUNT_METRICS, "pvToOrder")
_PAGINATION_FIELDS = ("pageSize", "pageNumber", "totalResults", "totalPages")
_DECIMAL_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_REQUEST_MARKER = re.compile(r"(?im)^\s*Request URL\s*:?\s*(https?://[^\r\n]+)?\s*$")


class WingError(Exception):
    """Base class for sanitized, user-facing WING failures."""


class AuthError(WingError):
    """The captured login is missing, invalid, or no longer authorized."""


class RateLimitError(WingError):
    """The endpoint returned HTTP 429; requests must stop without retry."""


class NetworkError(WingError):
    """A connection, response, or non-auth HTTP failure occurred."""


class TransientNetworkError(NetworkError):
    """A read-only request timed out or its connection was interrupted."""


class SchemaError(WingError):
    """A capture, configuration, or response failed strict validation."""


def _read_text(path: str | Path) -> str:
    try:
        with Path(path).open("rb") as source:
            value = source.read(MAX_CAPTURE_BYTES + 1)
        if len(value) > MAX_CAPTURE_BYTES:
            raise SchemaError("Capture exceeds the supported size limit.")
        return value.decode("utf-8-sig")
    except SchemaError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError):
        raise SchemaError("Capture could not be read as UTF-8 text.") from None


def _safe_header(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise AuthError("Required authentication headers are missing or invalid.")
    if len(value) > 65536 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuthError("Authentication headers contain unsupported characters.")
    return value


def _cookie_values(cookie: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        if not separator or not key:
            raise AuthError("Captured Cookie header is malformed.")
        if key in values:
            raise AuthError("Captured Cookie header contains duplicate names.")
        values[key] = value
    return values


def _auth_values(config: dict[str, Any]) -> tuple[str, str, str, str]:
    if not isinstance(config, dict):
        raise SchemaError("Store configuration must be an object.")
    store = config.get("store")
    if not isinstance(store, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", store):
        raise SchemaError("Store identifier is missing or invalid.")
    if config.get("currency", "KRW") != "KRW":
        raise SchemaError("This WING endpoint supports KRW only.")
    cookie = _safe_header(config.get("cookie"))
    xsrf = _safe_header(config.get("xsrf_token"))
    agent = _safe_header(config.get("user_agent") or DEFAULT_USER_AGENT)
    values = _cookie_values(cookie)
    if values.get("sc_vid") != store or not values.get("XSRF-TOKEN"):
        raise AuthError("Captured authentication does not match the store configuration.")
    encoded = values["XSRF-TOKEN"]
    if re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
        raise AuthError("Captured XSRF token is malformed.")
    try:
        decoded = unquote(encoded, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise AuthError("Captured XSRF token is malformed.") from None
    if decoded != xsrf:
        raise AuthError("Captured XSRF header does not match its Cookie value.")
    return store, cookie, xsrf, agent


def _capture_header(group: str, name: str) -> str | None:
    # DevTools plain-text copies put either "cookie: value" on one line or
    # "cookie[:]\nvalue" on two lines. Do not interpret arbitrary capture text.
    lines = group.splitlines()
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*(?::\s*(.*))?$", re.I)
    found: list[str] = []
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line)
        if not match:
            continue
        value = (match.group(1) or "").strip()
        if not value:
            if index + 1 >= len(lines):
                raise AuthError("Capture contains an incomplete authentication header.")
            value = lines[index + 1].strip()
        found.append(value)
    if len(found) > 1 and len(set(found)) != 1:
        raise AuthError("Capture contains conflicting authentication headers.")
    return found[0] if found else None


def read_capture(path: str | Path) -> list[dict[str, str]]:
    """Extract deduplicated store credentials from copied WING request groups.

    Only Request URL groups for the fixed API are accepted. Each group must have
    its own Cookie and X-XSRF-TOKEN; values from another request are never reused.
    No capture or credential text is included in exception messages.
    """
    text = _read_text(path)
    markers = list(_REQUEST_MARKER.finditer(text))
    if not markers:
        raise SchemaError("Capture contains no recognized Request URL groups.")
    configs: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        group = text[marker.end():end]
        url = marker.group(1)
        if url is None:
            url = next((line.strip() for line in group.splitlines() if line.strip()), "")
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            raise SchemaError("Capture contains an invalid request destination.") from None
        expected = urlsplit(API_URL)
        if (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment) != (
            expected.scheme, expected.netloc, expected.path, "", "",
        ):
            raise SchemaError("Capture request destination is not the supported WING endpoint.")
        cookie = _capture_header(group, "cookie")
        xsrf = _capture_header(group, "x-xsrf-token")
        agent = _capture_header(group, "user-agent") or DEFAULT_USER_AGENT
        if cookie is None or xsrf is None:
            raise AuthError("Each captured request requires its own Cookie and XSRF header.")
        cookie = _safe_header(cookie)
        store = _cookie_values(cookie).get("sc_vid", "")
        config = {"store": store, "cookie": cookie, "xsrf_token": xsrf, "user_agent": agent}
        _auth_values(config)
        if store not in seen:
            configs.append(config)
            seen.add(store)
    return configs


def _no_constant(_: str) -> None:
    raise ValueError("Unsupported JSON numeric constant.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _json_decoder() -> json.JSONDecoder:
    return json.JSONDecoder(parse_float=Decimal, parse_constant=_no_constant,
                            object_pairs_hook=_unique_object)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SchemaError("Response contains a non-finite number.")
        return str(value)
    if value is None or type(value) in (str, int, bool):
        return value
    raise SchemaError("Response field contains an unsupported value.")


def _sanitize_business_row(row: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(row, dict):
        raise SchemaError("Response business row must be an object.")
    details = row.get("vendorItemDetails")
    metrics = row.get("businessInsightsMetricsResponse")
    if not isinstance(details, dict) or not isinstance(metrics, dict):
        raise SchemaError("Response business row is missing details or metrics.")
    return {
        "vendorItemDetails": {key: _sanitize_value(details[key]) for key in _DETAIL_FIELDS if key in details},
        "businessInsightsMetricsResponse": {
            key: _sanitize_value(metrics[key]) for key in _METRIC_FIELDS if key in metrics
        },
    }


def extract_capture_response(path: str | Path) -> dict[str, Any]:
    """Return the first allowlisted API JSON response for offline inspection.

    This does not imply a complete page sequence or authorize marking a sales
    window complete. Headers and unknown object fields are never returned.
    """
    text = _read_text(path)
    decoder = _json_decoder()
    offset = 0
    while offset < len(text):
        start = text.find("{", offset)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, InvalidOperation, RecursionError):
            offset = start + 1
            continue
        offset = end
        if not isinstance(value, dict) or "vendorItems" not in value:
            continue
        rows, pagination = value.get("vendorItems"), value.get("paginationDetails")
        if not isinstance(rows, list) or not isinstance(pagination, dict):
            raise SchemaError("Captured response is missing business rows or pagination.")
        return {
            "vendorItems": [_sanitize_business_row(row) for row in rows],
            "soldVICount": _sanitize_value(value.get("soldVICount")),
            "paginationDetails": {
                key: _sanitize_value(pagination[key]) for key in _PAGINATION_FIELDS if key in pagination
            },
        }
    raise SchemaError("Capture contains no recognized WING business response.")


def _count(value: Any, field: str | None = None) -> int:
    # Field labels come from fixed schema keys, never request or response values.
    allowed = (*_COUNT_METRICS, "totalUnitsSold", "soldVICount", *_PAGINATION_FIELDS)
    label = field if field in allowed else "count"
    lower = -(10**18) if field in {"totalUnitsSold", "totalOrders"} else 0
    message = f"Response {label} must be a bounded {'integer' if lower < 0 else 'nonnegative integer'}."
    if type(value) is int and lower <= value <= 10**18:
        return value
    if type(value) in (str, Decimal):
        representation = str(value)
        if len(representation) <= 128 and _DECIMAL_RE.fullmatch(representation):
            try:
                number = Decimal(representation)
            except InvalidOperation:
                raise SchemaError(message) from None
            if (number.is_finite() and lower <= number <= 10**18
                    and number == number.to_integral_value()):
                return int(number)
    raise SchemaError(message)


def _decimal(value: Any, *, signed: bool = False) -> str:
    if type(value) not in (int, str, Decimal):
        raise SchemaError("Response amount or ratio must be a finite decimal.")
    representation = str(value)
    if len(representation) > 128 or not _DECIMAL_RE.fullmatch(representation):
        raise SchemaError("Response amount or ratio must be a finite nonnegative decimal.")
    try:
        number = Decimal(representation)
    except InvalidOperation:
        raise SchemaError("Response amount or ratio is malformed.") from None
    if not number.is_finite() or (number < 0 and not signed) or abs(number.adjusted()) > 100:
        raise SchemaError("Response amount or ratio is outside supported bounds.")
    return format(number, "f")


def _identifier(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is int and value >= 0:
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SchemaError("Response identifier is missing or invalid.")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SchemaError("Response identifier contains unsupported characters.")
    return value


def _normalize_row(value: Any, store: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = _sanitize_business_row(value)
    details, metrics = raw["vendorItemDetails"], raw["businessInsightsMetricsResponse"]
    if set(_DETAIL_FIELDS) - set(details) or {"totalUnitsSold", "totalGmv"} - set(metrics):
        raise SchemaError("Response business row is missing required fields.")
    if _identifier(details["vendorId"]) != store:
        raise SchemaError("Response contains a different store identifier.")
    name = details["itemName"] or details["productName"]
    if not isinstance(name, str) or not name.strip() or len(name) > 10000:
        raise SchemaError("Response product name is missing or invalid.")
    registration = details["registrationType"]
    if registration not in ("NORMAL", "RFM"):
        raise SchemaError("Response registration type is unsupported.")
    if type(details["isOOS"]) is not bool:
        raise SchemaError("Response out-of-stock flag must be Boolean.")
    normalized = {
        "sku": _identifier(details["vendorItemId"]),
        "name": name,
        "units": _count(metrics["totalUnitsSold"], "totalUnitsSold"),
        "amount": _decimal(metrics["totalGmv"], signed=True),
        "traffic": {
            **{key: _count(metrics[key], key) if metrics.get(key) is not None else None
               for key in _COUNT_METRICS},
            "pvToOrder": _decimal(metrics["pvToOrder"], signed=True) if metrics.get("pvToOrder") is not None else None,
        },
        "registration_type": registration,
        "is_oos": details["isOOS"],
        "inventory_id": _identifier(details["inventoryId"], nullable=True),
    }
    return normalized, raw


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _default_transport(request: Request, timeout: float) -> Any:
    # The normal HTTPS handler verifies certificates using the system trust
    # store. No context disabling verification or redirect forwarding is used.
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _status_error(status: int) -> None:
    if type(status) is not int or not 100 <= status <= 599:
        raise NetworkError("WING response has an invalid HTTP status.") from None
    if status in (401, 403):
        raise AuthError(f"WING authentication failed (HTTP {status}); refresh the authorized capture.") from None
    if 300 <= status < 400:
        raise AuthError(f"WING redirected (HTTP {status}); refresh the authorized capture.") from None
    if status == 429:
        raise RateLimitError("WING rate limit reached (HTTP 429); collection stopped without retry.") from None
    if status != 200:
        raise NetworkError(f"WING returned HTTP {status}; collection stopped.") from None


def _response_json(response: Any) -> dict[str, Any]:
    try:
        status = response.status
        if type(status) is not int:
            raise NetworkError("WING response has an invalid HTTP status.")
        _status_error(status)
        if hasattr(response, "geturl") and response.geturl() != API_URL:
            raise AuthError("WING response redirected; refresh the authorized capture.")
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" in content_type:
            raise AuthError("WING returned a login or HTML page; refresh the authorized capture.")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise NetworkError("WING response is missing or exceeds the supported size limit.")
        try:
            text = body.decode("utf-8-sig")
        except UnicodeError:
            raise SchemaError("WING response is not UTF-8 JSON.") from None
        if text.lstrip().startswith("<"):
            raise AuthError("WING returned a login or HTML page; refresh the authorized capture.")
        try:
            value, end = _json_decoder().raw_decode(text.lstrip())
            if text.lstrip()[end:].strip():
                raise ValueError("Trailing data")
        except (ValueError, InvalidOperation, RecursionError):
            raise SchemaError("WING response is malformed JSON.") from None
        if not isinstance(value, dict):
            raise SchemaError("WING response must be a JSON object.")
        if "vendorItems" not in value:
            indicators = ("login", "log-in", "sign-in", "signin", "unauthorized",
                          "unauthenticated", "session expired", "session_expired", "로그인")
            for key in ("code", "status", "message", "error", "errorCode", "redirectUrl", "loginUrl"):
                candidate = value.get(key)
                if isinstance(candidate, str) and any(term in candidate.casefold() for term in indicators):
                    raise AuthError("WING returned a login response; refresh the authorized capture.")
        return value
    except WingError:
        raise
    except (TimeoutError, ConnectionError):
        raise TransientNetworkError("WING response connection was interrupted.") from None
    except Exception:
        raise NetworkError("WING response could not be read.") from None
    finally:
        if hasattr(response, "close"):
            try:
                response.close()
            except Exception:
                pass


def _perform(request: Request, transport: Callable[[Request, float], Any]) -> dict[str, Any]:
    try:
        response = transport(request, TIMEOUT_SECONDS)
    except HTTPError as error:
        try:
            _status_error(error.code)
        finally:
            try:
                error.close()
            except Exception:
                pass
        raise NetworkError("WING returned an unsuccessful HTTP status.") from None
    except WingError:
        raise
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, ConnectionError)):
            raise TransientNetworkError("WING connection timed out or was interrupted.") from None
        raise NetworkError("WING connection failed; collection stopped.") from None
    except (TimeoutError, ConnectionError):
        raise TransientNetworkError("WING connection timed out or was interrupted.") from None
    except Exception:
        raise NetworkError("WING connection failed; collection stopped.") from None
    return _response_json(response)


def _analysis_date(value: str | date | None) -> date:
    if value is None:
        return datetime.now(KST).date()
    try:
        if type(value) is date:
            result = value
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            result = date.fromisoformat(value)
        else:
            raise ValueError("Invalid date")
        if result > datetime.now(KST).date():
            raise ValueError("Future date")
        # All three windows must be representable, including the 30-day range.
        result - timedelta(days=30)
        return result
    except (ValueError, OverflowError):
        raise SchemaError("Analysis date must be a valid YYYY-MM-DD date no later than today in Seoul.") from None


def fetch_store(config: dict[str, Any], analysis_date: str | date | None = None,
                transport: Callable[[Request, float], Any] | None = None,
                request_interval: float = 0.5, max_pages: int = 200,
                progress: Callable[[dict[str, Any]], Any] | None = None,
                network_retries: int = 2) -> dict[str, Any]:
    """Collect three fully validated windows, ending before the analysis date.

    Only connection interruptions/timeouts get bounded read-only retries; no
    retry follows authentication, rate-limit, HTTP, or schema errors. No
    partially collected dataset is returned. Pagination totals and row counts
    must agree on every page; duplicate SKUs and mixed-store rows are rejected.
    ``progress`` receives only store/window/page/total_pages after each page's
    metadata validates and before business rows validate, for private diagnostics.
    """
    store, cookie, xsrf, agent = _auth_values(config)
    day = _analysis_date(analysis_date)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise SchemaError("Maximum pages must be an integer between 1 and 1000.")
    if type(network_retries) is not int or not 0 <= network_retries <= 3:
        raise SchemaError("Network retries must be an integer between 0 and 3.")
    if type(request_interval) not in (int, float) or not math.isfinite(request_interval) or not 0 <= request_interval <= 60:
        raise SchemaError("Request interval must be a finite number between 0 and 60 seconds.")
    sender = transport or _default_transport
    if not callable(sender):
        raise SchemaError("Transport must be callable.")
    if progress is not None and not callable(progress):
        raise SchemaError("Progress callback must be callable.")
    ranges = {
        "last_30d": (day - timedelta(days=30), day - timedelta(days=1)),
        "current_7d": (day - timedelta(days=7), day - timedelta(days=1)),
        "previous_7d": (day - timedelta(days=14), day - timedelta(days=8)),
    }
    windows: dict[str, Any] = {}
    raw_windows: dict[str, Any] = {}
    requested = False
    for window_name, (start, end) in ranges.items():
        rows: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        baseline: tuple[int, int, int] | None = None
        page = 0
        while True:
            if page >= max_pages:
                raise SchemaError("WING pagination exceeds the configured page limit.")
            payload = {
                "startDate": start.isoformat(), "endDate": end.isoformat(),
                "registrationTypes": ["NORMAL", "RFM"], "pageNumber": page,
                "pageSize": PAGE_SIZE, "sortBy": "GMV", "sortOrder": "DESC",
                "includeSoldVICount": True,
            }
            request = Request(API_URL, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                              headers={"Cookie": cookie, "X-XSRF-TOKEN": xsrf,
                                       "User-Agent": agent, "Origin": ORIGIN, "Referer": REFERER,
                                       "Content-Type": "application/json", "Accept": "application/json"},
                              method="POST")
            if requested and request_interval:
                time.sleep(request_interval)
            requested = True
            for attempt in range(network_retries + 1):
                try:
                    response = _perform(request, sender)
                    break
                except TransientNetworkError:
                    if attempt == network_retries:
                        raise
                    time.sleep(max(request_interval, 1) * (attempt + 1))
            pagination = response.get("paginationDetails")
            items = response.get("vendorItems")
            if not isinstance(pagination, dict) or not isinstance(items, list):
                raise SchemaError("WING response is missing rows or pagination details.")
            if set(_PAGINATION_FIELDS) - set(pagination) or "soldVICount" not in response:
                raise SchemaError("WING response is missing required pagination fields.")
            size, number, total, pages = (_count(pagination[key], key) for key in _PAGINATION_FIELDS)
            sold = _count(response["soldVICount"], "soldVICount")
            computed_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            if size != PAGE_SIZE or number != page or (pages != computed_pages and not (total == 0 and pages == 1)):
                raise SchemaError("WING pagination number, size, or total page count is inconsistent.")
            if pages > max_pages or sold > total:
                raise SchemaError("WING pagination totals exceed supported bounds.")
            current = (total, pages, sold)
            if baseline is None:
                baseline = current
            elif current != baseline:
                raise SchemaError("WING pagination totals changed during collection.")
            if progress is not None:
                try:
                    progress({"store": store, "window": window_name,
                              "page": page, "total_pages": pages})
                except Exception:
                    raise SchemaError("Collection progress callback failed.") from None
            expected_count = min(PAGE_SIZE, max(0, total - page * PAGE_SIZE))
            if len(items) != expected_count:
                raise SchemaError("WING response has a missing or extra page row.")
            for item in items:
                normalized, raw = _normalize_row(item, store)
                if normalized["sku"] in seen:
                    raise SchemaError("WING response contains a duplicate SKU within a window.")
                seen.add(normalized["sku"])
                rows.append(normalized)
                raw_rows.append(raw)
            page += 1
            if page >= pages:
                break
        if baseline is None or len(rows) != baseline[0]:
            raise SchemaError("WING total result count does not match collected rows.")
        captured_at = datetime.now(KST).isoformat(timespec="seconds")
        windows[window_name] = {
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "complete": True, "rows": rows, "captured_at": captured_at,
        }
        raw_windows[window_name] = raw_rows
    return {
        "store": store, "timezone": "Asia/Seoul", "currency": "KRW",
        "analysis_date": day.isoformat(), "observed_at": datetime.now(KST).isoformat(timespec="seconds"),
        "sales_basis": "platform_reported", "sales_basis_note": SALES_BASIS_NOTE,
        "sales_windows": windows, "raw_windows": raw_windows,
    }
