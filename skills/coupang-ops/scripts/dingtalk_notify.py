"""Private DingTalk enterprise work notices, using only Python's standard library.

Configuration is read as data; ERP modules are never imported or executed.
Returned configured dictionaries contain credentials and must not be logged.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_RECIPIENT = "倪笑缘"
TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
SEND_URL = "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"
PROGRESS_URL = "https://oapi.dingtalk.com/topapi/message/corpconversation/getsendprogress"
RESULT_URL = "https://oapi.dingtalk.com/topapi/message/corpconversation/getsendresult"
_URLS = {TOKEN_URL, SEND_URL, PROGRESS_URL, RESULT_URL}
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", "outputs"}
_FIELDS = {
    "app_key": {"appkey", "dingtalkappkey", "dingappkey"},
    "app_secret": {"appsecret", "dingtalkappsecret", "dingappsecret"},
    "agent_id": {"agentid", "dingtalkagentid", "dingagentid"},
    "recipient_name": {"recipientname", "dingtalkrecipientname", "name", "username", "nickname"},
    "user_ids": {"userids", "userid", "useridlist", "dingtalkuserid", "dingtalkuserids", "useruuid"},
}
Transport = Callable[..., dict[str, Any]]


def _normal(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _field(record: dict, field: str) -> Any:
    values = [value for key, value in record.items() if _normal(key) in _FIELDS[field]]
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _user_ids(value: Any) -> list[str] | None:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return None
    # A single named person must have exactly one explicitly resolved userId.
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    value = values[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:@+-]{1,128}", value):
        return None
    return [value]


def _missing(recipient: str, fields: list[str], reason: str) -> dict:
    return {"status": "missing_config", "recipient_name": recipient, "missing": fields, "reason": reason}


def _literal(node: ast.AST, values: dict) -> Any:
    """Evaluate literals, prior literal names, and explicit environment lookups only."""
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.Dict):
        return {_literal(k, values): _literal(v, values) for k, v in zip(node.keys, node.values) if k is not None}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(item, values) for item in node.elts]
    if isinstance(node, ast.Call) and ast.unparse(node.func) in {"os.getenv", "os.environ.get"}:
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            default = _literal(node.args[1], values) if len(node.args) > 1 else None
            return os.environ.get(node.args[0].value, default)
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _python_values(source: str) -> dict:
    tree = ast.parse(source)
    values: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = _literal(node.value, values)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values[node.target.id] = _literal(node.value, values)
    # The existing ERP explicitly binds this environment override to its named
    # recipient. Its database fallback requires a separate read-only resolution.
    if values.get("RECIPIENT_NAME") and "OZON_SALES_DINGTALK_USER_ID" in source:
        override = os.environ.get("OZON_SALES_DINGTALK_USER_ID")
        if override is not None:
            values["user_ids"] = override
    return values


def _records(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _records(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _records(child)


def _read_data(path: Path) -> Any:
    source = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".py":
        return _python_values(source)
    if path.suffix.lower() == ".json":
        return json.loads(source)
    # Deliberately support plain key=value configuration without interpolation.
    if path.suffix.lower() == ".properties" or path.name == ".env" or path.name.startswith(".env."):
        result = {}
        for line in source.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip().removeprefix("export ")] = value.strip().strip("\"'")
        return result
    return {}


def _config_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS
                         and not (Path(directory) / name).is_symlink())
        for name in names:
            item = Path(directory) / name
            if not item.is_symlink() and (item.suffix.lower() in {".py", ".json", ".properties"}
                                          or item.name.startswith(".env")):
                files.append(item)
    return sorted(files)


def load_erp_config(path: str | Path, recipient_name: str = DEFAULT_RECIPIENT,
                    recipient_user_ids: list[str] | None = None) -> dict:
    """Read an ERP directory or explicit private configuration without sending.

    Require one unambiguous application and one exact name-to-userId binding.
    Missing/ambiguous results intentionally contain no credential values.
    """
    root = Path(path)
    if not isinstance(recipient_name, str) or not recipient_name.strip():
        return _missing(DEFAULT_RECIPIENT, ["recipient_name"], "recipient_required")
    if not root.exists():
        return _missing(recipient_name, ["app_key", "app_secret", "agent_id", "user_ids"], "config_not_found")
    explicit_ids = _user_ids(recipient_user_ids) if recipient_user_ids is not None else None
    if recipient_user_ids is not None and explicit_ids is None:
        return _missing(recipient_name, ["user_ids"], "exact_recipient_required")
    files = _config_files(root)
    applications: set[tuple[str, str, int]] = set()
    recipients: set[str] = set()
    ambiguous_recipient = False
    for item in files:
        try:
            data = _read_data(item)
        except (OSError, UnicodeError, ValueError, SyntaxError, TypeError):
            continue
        for record in _records(data):
            key, secret, agent = (_field(record, name) for name in ("app_key", "app_secret", "agent_id"))
            if isinstance(key, str) and key.strip() and isinstance(secret, str) and secret.strip():
                try:
                    agent_id = int(agent)
                except (ValueError, TypeError):
                    agent_id = 0
                if agent_id > 0:
                    applications.add((key.strip(), secret.strip(), agent_id))
            if explicit_ids is None and _field(record, "recipient_name") == recipient_name:
                ids = _user_ids(_field(record, "user_ids"))
                if ids:
                    recipients.add(ids[0])
                elif _field(record, "user_ids") is not None:
                    ambiguous_recipient = True
            # Explicit maps such as {'recipients': {'倪笑缘': 'staff-id'}}.
            if explicit_ids is None and recipient_name in record:
                target = record[recipient_name]
                ids = _user_ids(_field(target, "user_ids") if isinstance(target, dict) else target)
                if ids:
                    recipients.add(ids[0])
                elif target is not None:
                    ambiguous_recipient = True
    if explicit_ids:
        recipients = set(explicit_ids)
    missing = []
    if len(applications) != 1:
        missing.extend(["app_key", "app_secret", "agent_id"])
    if len(recipients) != 1 or ambiguous_recipient:
        missing.append("user_ids")
    if missing:
        reason = "ambiguous_config" if len(applications) > 1 or len(recipients) > 1 or ambiguous_recipient else "required_config_missing"
        return _missing(recipient_name, missing, reason)
    key, secret, agent = next(iter(applications))
    return {"status": "configured", "app_key": key, "app_secret": secret, "agent_id": agent,
            "user_ids": list(recipients), "recipient_name": recipient_name}


def resolve_erp_user_id(path: str | Path, recipient_name: str = DEFAULT_RECIPIENT) -> dict:
    """Resolve one ERP user with a parameterized, read-only name query.

    This is an explicit network action separate from load_erp_config. It uses
    only existing ERP database settings and never imports any ERP module.
    """
    if not isinstance(recipient_name, str) or not recipient_name.strip():
        return _missing(DEFAULT_RECIPIENT, ["user_ids"], "recipient_required")
    root = Path(path)
    config_path = root / "config" / "config.py" if root.is_dir() else root
    try:
        values = _python_values(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, SyntaxError, TypeError):
        return _missing(recipient_name, ["user_ids"], "erp_database_config_unavailable")
    if not all(isinstance(values.get(field), str) and values[field].strip()
               for field in ("USER", "PASSWORD", "DATABASE", "IPADDRESS")):
        return _missing(recipient_name, ["user_ids"], "erp_database_config_missing")
    try:
        import pymysql
    except ImportError:
        return _missing(recipient_name, ["user_ids"], "pymysql_unavailable")
    address = values["IPADDRESS"].strip()
    try:
        host, port_text = address.rsplit(":", 1)
        port = int(port_text)
        if not host or not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return _missing(recipient_name, ["user_ids"], "erp_database_address_invalid")
    connection = None
    cursor = None
    try:
        connection = pymysql.connect(host=host, port=port, user=values["USER"],
                                     password=unquote(values["PASSWORD"]), database=values["DATABASE"],
                                     charset="utf8mb4", connect_timeout=5, read_timeout=10,
                                     write_timeout=5, autocommit=True)
        cursor = connection.cursor()
        cursor.execute("SELECT user_uuid FROM `user` WHERE (nick_name=%s OR user_name=%s) "
                       "AND active=1 AND erp_active=1 LIMIT 2", (recipient_name, recipient_name))
        rows = cursor.fetchall()
        if len(rows) != 1:
            reason = "erp_recipient_not_found" if not rows else "erp_recipient_ambiguous"
            return _missing(recipient_name, ["user_ids"], reason)
        row = rows[0]
        value = row.get("user_uuid") if isinstance(row, dict) else row[0]
        ids = _user_ids(value)
        if ids is None:
            return _missing(recipient_name, ["user_ids"], "erp_recipient_user_id_invalid")
        return {"status": "resolved", "recipient_name": recipient_name, "user_id": ids[0]}
    except Exception:
        return _missing(recipient_name, ["user_ids"], "erp_database_lookup_failed")
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def _validated(config: Any) -> dict | None:
    if not isinstance(config, dict) or config.get("status") == "missing_config":
        return None
    if not isinstance(config.get("recipient_name"), str) or not config["recipient_name"].strip():
        return None
    if not all(isinstance(config.get(key), str) and config[key].strip() for key in ("app_key", "app_secret")):
        return None
    ids = _user_ids(config.get("user_ids"))
    try:
        agent = int(config.get("agent_id"))
    except (ValueError, TypeError):
        return None
    if not ids or agent <= 0 or config.get("to_all_user") or config.get("dept_id_list"):
        return None
    return {"app_key": config["app_key"], "app_secret": config["app_secret"], "agent_id": agent,
            "user_ids": ids, "recipient_name": config["recipient_name"]}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _stdlib_transport(method: str, url: str, payload: dict, timeout: int = 15) -> dict:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"api.dingtalk.com", "oapi.dingtalk.com"}:
        raise ValueError("endpoint_not_allowed")
    if url.split("?", 1)[0] not in _URLS:
        raise ValueError("endpoint_not_allowed")
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method=method,
                      headers={"Content-Type": "application/json; charset=utf-8"})
    # No ambient proxy, cookies, or redirects can forward these credentials.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return {"_transport_error": "invalid_response"}
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {"_transport_error": "invalid_response"}
    except HTTPError as error:
        # Never return HTTP exception strings, URLs, or response bodies.
        return {"_transport_error": "http_error", "_http_status": error.code}


def _request(transport: Transport, url: str, payload: dict, stage: str, uncertain: bool = False):
    try:
        response = transport("POST", url, payload, timeout=15)
    except Exception:
        return None, {"status": "unknown" if uncertain else "failed", "stage": stage, "error": "transport_error"}
    if not isinstance(response, dict):
        return None, {"status": "unknown" if uncertain else "failed", "stage": stage, "error": "invalid_response"}
    if response.get("_transport_error"):
        result = {"status": "unknown" if uncertain else "failed", "stage": stage, "error": "http_error"}
        code = response.get("_http_status")
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
            result["http_status"] = code
        return None, result
    code = response.get("errcode", response.get("code"))
    if stage != "token" and ("errcode" not in response or isinstance(response["errcode"], bool)):
        return None, {"status": "unknown" if uncertain else "failed", "stage": stage, "error": "invalid_response"}
    if code not in (None, 0, "0"):
        result = {"status": "failed", "stage": stage, "error": "api_error"}
        # Raw errmsg/message and OAuth string codes may reflect credentials.
        if isinstance(code, int) and not isinstance(code, bool):
            result["code"] = code
        return None, result
    return response, None


def _token(config: dict, transport: Transport):
    response, error = _request(transport, TOKEN_URL, {"appKey": config["app_key"], "appSecret": config["app_secret"]}, "token")
    if error:
        return None, error
    token = response.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return None, {"status": "failed", "stage": "token", "error": "invalid_response"}
    return token, None


def _sensitive_message(title: str, text: str, config: dict) -> bool:
    combined = title + "\n" + text
    if config["app_secret"] in combined or config["app_key"] in combined:
        return True
    return bool(re.search(r"(?i)(?:cookie|authorization|app[_-]?secret|access[_-]?token)\s*[:=]", combined))


def send_work_notice(config: dict, title: str, text: str, transport: Transport | None = None) -> dict:
    """Submit once. accepted is only an async acknowledgement, not delivery.

    A send timeout returns unknown. The caller must persist it and must not
    retry that message automatically, since DingTalk may have accepted it.
    """
    config = _validated(config)
    if config is None:
        return {"status": "missing_config", "error": "exact_recipient_and_application_required"}
    if not isinstance(title, str) or not title.strip() or not isinstance(text, str) or not text.strip():
        return {"status": "failed", "stage": "message", "error": "message_required"}
    if _sensitive_message(title, text, config):
        return {"status": "failed", "stage": "message", "error": "sensitive_content_blocked"}
    transport = transport or _stdlib_transport
    token, error = _token(config, transport)
    if error:
        return error
    if token in title + "\n" + text:
        return {"status": "failed", "stage": "message", "error": "sensitive_content_blocked"}
    payload = {"agent_id": config["agent_id"], "userid_list": ",".join(config["user_ids"]), "to_all_user": False,
               "msg": {"msgtype": "markdown", "markdown": {"title": title, "text": text}}}
    response, error = _request(transport, SEND_URL + "?" + urlencode({"access_token": token}), payload, "send", uncertain=True)
    if error:
        return error
    task_id = response.get("task_id")
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return {"status": "unknown", "stage": "send", "error": "task_id_missing"}
    return {"status": "accepted", "task_id": task_id}


def verify_delivery(config: dict, task_id: int, transport: Transport | None = None) -> dict:
    """Query an existing task; this function never creates or retries messages."""
    config = _validated(config)
    if config is None:
        return {"status": "missing_config", "error": "exact_recipient_and_application_required"}
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return {"status": "unknown", "error": "invalid_task_id"}
    transport = transport or _stdlib_transport
    token, error = _token(config, transport)
    if error:
        return {**error, "task_id": task_id}
    query = "?" + urlencode({"access_token": token})
    payload = {"agent_id": config["agent_id"], "task_id": task_id}
    response, error = _request(transport, PROGRESS_URL + query, payload, "progress")
    if error:
        return {**error, "task_id": task_id}
    progress = response.get("progress")
    if not isinstance(progress, dict) or progress.get("status") not in (0, 1, 2):
        return {"status": "unknown", "task_id": task_id, "error": "invalid_response"}
    if progress.get("status") != 2:
        return {"status": "pending", "task_id": task_id}
    response, error = _request(transport, RESULT_URL + query, payload, "result")
    if error:
        return {**error, "task_id": task_id}
    result = response.get("send_result")
    if not isinstance(result, dict):
        return {"status": "unknown", "task_id": task_id, "error": "invalid_response"}
    target = set(config["user_ids"])
    failed = set()
    received = set()
    read = set()
    for field in ("invalid_user_id_list", "forbidden_user_id_list", "failed_user_id_list"):
        if isinstance(result.get(field), list):
            failed.update(value for value in result[field] if isinstance(value, str))
    for entry in result.get("forbidden_list", []) if isinstance(result.get("forbidden_list", []), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("userid"), str):
            failed.add(entry["userid"])
    for field in ("read_user_id_list", "unread_user_id_list"):
        if isinstance(result.get(field), list):
            received.update(value for value in result[field] if isinstance(value, str))
            if field == "read_user_id_list":
                read.update(value for value in result[field] if isinstance(value, str))
    if target & failed:
        return {"status": "failed", "task_id": task_id, "error": "recipient_delivery_failed"}
    if target <= received:
        return {"status": "delivered", "task_id": task_id, "read": target <= read}
    return {"status": "unknown", "task_id": task_id, "error": "recipient_result_missing"}
