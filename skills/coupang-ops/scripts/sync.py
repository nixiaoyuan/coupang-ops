"""Four-store WING refresh, operations reports and DingTalk work notices.

Private configuration and results belong outside public commits. Core collection
uses the standard library. XLSX uses the Codex bundled Artifact Tool runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wing_client import AuthError, WingError, fetch_store, read_capture
from operations import analyze_store, render_report
from mysql_sink import persist_refresh

KST = timezone(timedelta(hours=9))
SCRIPTS = Path(__file__).resolve().parent


def load_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def private_config_path(path):
    """Resolve traversal and links before requiring an actual private parent."""
    target = Path(path).resolve()
    if "private" not in target.parent.parts or target.is_dir():
        raise ValueError("认证配置必须是 private 目录内的文件，避免进入 Git")
    ensure_git_private(target)
    return target


def ensure_git_private(target):
    """Refuse tracked or unignored authentication files; never edit Git config."""
    # An uncreated private directory can still be below a Git checkout.
    existing = target.parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        probe = subprocess.run(["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=15)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Without Git, a .git ancestor is sufficient to require manual repair.
        if any((parent / ".git").exists() for parent in (existing, *existing.parents)):
            raise ValueError("无法核验认证文件的 Git 私有目录保护") from None
        return
    if probe.returncode:
        if any((parent / ".git").exists() for parent in (existing, *existing.parents)):
            raise ValueError("无法核验认证文件的 Git 私有目录保护")
        return
    try:
        root = Path(probe.stdout.strip()).resolve()
        relative = target.relative_to(root).as_posix()
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
                                 capture_output=True, timeout=15)
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "--", relative],
                                 capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("无法核验认证文件的 Git 私有目录保护") from None
    if tracked.returncode == 0:
        raise ValueError("认证配置已被 Git 跟踪，请先将私有配置移出跟踪范围")
    if tracked.returncode != 1 or ignored.returncode != 0:
        raise ValueError("认证配置未被 Git 忽略，不能写入认证信息")


def cache_path(output, store):
    if not isinstance(store, str) or not store:
        raise ValueError("店铺标识缺失")
    identity = hashlib.sha256(store.encode("utf-8")).hexdigest()
    return Path(output) / f"store-{identity}.json"


def extras_path(directory, store):
    """Never interpret a source-provided store identifier as a filesystem path."""
    if (not isinstance(store, str) or not store or store in {".", ".."}
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', store) or store.endswith((".", " "))):
        raise ValueError("店铺标识不能用作附加数据文件名")
    if store.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("店铺标识不能用作附加数据文件名")
    root = Path(directory).resolve()
    candidate = (root / (store + ".json")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("附加数据路径超出指定目录") from None
    return candidate


def cached_dataset(path, store):
    old = load_json(path)
    expected = {"last_30d", "current_7d", "previous_7d"}
    if (not isinstance(old, dict) or old.get("store") != store
            or not isinstance(old.get("sales_windows"), dict)
            or set(old["sales_windows"]) != expected
            or any(not isinstance(window, dict) or window.get("complete") is not True
                   for window in old["sales_windows"].values())):
        raise ValueError("旧快照无效或不是完整店铺窗口")
    # Validate dates, metrics and row shapes before passing cached data to CSV.
    analyze_store(old)
    old["refresh_status"] = "stale"
    return old


def stale_summary(summary):
    for key in ("ranking", "sales_drop", "stock_risk", "slow_stock", "returns"):
        section = summary[key]
        section["status"] = "incomplete"
        section["missing"] = sorted(set(section["missing"] + ["本次采集失败：仅保留上次成功快照，无法判断本次状态"]))
        for row in section["rows"]:
            row["historical_alert"] = row.get("alert")
            row["alert"] = None
    summary["sales_basis_note"] += f"；本次采集失败，仅保留历史快照，采集观察截止 {summary['observed_at']}，不能代表本次更新。"
    summary["limitations"].append("历史销售快照不与本次附加数据拼接；历史阈值结果保存在 historical_alert，本次预警未知。")
    return summary


def notification_complete(result):
    if not isinstance(result, dict):
        return False
    if result.get("status") == "delivered":
        return True
    if result.get("status") != "accepted":
        return False
    # Pending is an explicit async acknowledgement, never a delivery claim.
    delivery = result.get("delivery")
    return isinstance(delivery, dict) and delivery.get("status") in {"delivered", "pending"}


def safe_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@contextmanager
def lock_guard(output):
    """Serialize lock recovery with a kernel lock released automatically on exit."""
    stream = (output / ".refresh.guard").open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("已有更新正在运行，未修改当前运行锁") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def process_state(pid):
    """Inspect identity read-only; unknown permission is never proof of death.

    Windows os.kill(pid, 0) can terminate a process and is deliberately unused.
    ``process_start`` is the creation FILETIME in 100ns ticks on Windows.
    """
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return {"status": "unknown"}
    if os.name != "nt":
        # Linux procfs is read-only and supplies a stable process creation time.
        proc = Path("/proc") / str(pid)
        try:
            stat = (proc / "stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2:].split()
            if fields[0] in {"Z", "X"}:
                return {"status": "dead"}
            ticks = int(fields[19])
            boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text(encoding="ascii").splitlines() if line.startswith("btime "))
            start = (boot + 11644473600) * 10000000 + ticks * 10000000 // os.sysconf("SC_CLK_TCK")
            return {"status": "alive", "process_start": start}
        except FileNotFoundError:
            return {"status": "dead"} if Path("/proc").is_dir() and not proc.exists() else {"status": "unknown"}
        except (OSError, ValueError, IndexError, StopIteration):
            return {"status": "unknown"}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return {"status": "dead"} if ctypes.get_last_error() == 87 else {"status": "unknown"}
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return {"status": "unknown"}
        if exit_code.value != 259:  # STILL_ACTIVE
            return {"status": "dead"}
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return {"status": "unknown"}
        start = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return {"status": "alive", "process_start": start} if start > 0 else {"status": "unknown"}
    finally:
        kernel.CloseHandle(handle)


def lock_owner(path):
    raw = path.read_bytes()
    if len(raw) > 4096:
        raise ValueError("运行锁格式无效；保留原锁，需人工核验")
    try:
        text = raw.decode("ascii").strip()
        if re.fullmatch(r"[0-9]+", text):
            owner = {"pid": int(text)}
        else:
            def distinct(pairs):
                record = {}
                for key, value in pairs:
                    if key in record:
                        raise ValueError("duplicate lock field")
                    record[key] = value
                return record
            owner = json.loads(text, object_pairs_hook=distinct)
            if not isinstance(owner, dict) or set(owner) != {"pid", "process_start"}:
                raise ValueError("invalid lock fields")
            if type(owner["process_start"]) is not int or not 0 < owner["process_start"] <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError("invalid process creation time")
        if type(owner["pid"]) is not int or not 0 < owner["pid"] <= 0xFFFFFFFF:
            raise ValueError("invalid pid")
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("运行锁格式无效；保留原锁，需人工核验") from None
    return owner


@contextmanager
def run_lock(output):
    output = Path(output)
    path = output / ".refresh.lock"
    output.mkdir(parents=True, exist_ok=True)
    with lock_guard(output):
        if path.exists():
            owner = lock_owner(path)
            state = process_state(owner["pid"])
            reused = (state.get("status") == "alive" and "process_start" in owner
                      and type(state.get("process_start")) is int
                      and state["process_start"] != owner["process_start"])
            if state.get("status") == "dead" or reused:
                path.unlink()  # Guard excludes another recovering/new runner.
            else:
                raise ValueError("已有更新正在运行，或无法核实旧锁拥有进程；保留原锁")
        pid = os.getpid()
        current = process_state(pid)
        if current.get("status") != "alive" or type(current.get("process_start")) is not int or current["process_start"] <= 0:
            raise ValueError("无法核验当前进程起始时间，未创建运行锁")
        payload = json.dumps({"pid": pid, "process_start": current["process_start"]}, separators=(",", ":")).encode("ascii")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            try:
                if path.read_bytes() == payload:
                    path.unlink()
            except OSError:
                pass  # A later run may recover only after proving this owner dead.


def initialize(args):
    target = private_config_path(args.config)
    if target.exists() and not args.replace:
        raise ValueError("配置已存在；更新 Cookie 时明确使用 --replace")
    stores = read_capture(args.capture)
    config = {
        "schema_version": 2,
        "stores": stores,
        "dingtalk_erp_path": args.erp_config,
        "recipient_name": "倪笑缘",
        "request_interval": 0.5,
        "max_pages": 200,
        "thresholds": {},
        "mysql_enabled": False,
    }
    atomic_json(target, config)
    print(json.dumps({"status": "configured", "store_count": len(stores)}, ensure_ascii=False))


def notice_config(config):
    from dingtalk_notify import load_erp_config
    return load_erp_config(
        config.get("dingtalk_erp_path", ""),
        recipient_name=config.get("recipient_name", "倪笑缘"),
        recipient_user_ids=config.get("dingtalk_user_ids"),
    )


def resolve_recipient(args):
    from dingtalk_notify import resolve_erp_user_id
    target = private_config_path(args.config)
    config = load_json(target)
    result = resolve_erp_user_id(config.get("dingtalk_erp_path", ""), config.get("recipient_name", "倪笑缘"))
    if result.get("status") == "resolved":
        config["dingtalk_user_ids"] = [result["user_id"]]
        atomic_json(target, config)
        print(json.dumps({"status": "configured", "recipient": config.get("recipient_name")}, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False))
        return 2
    return 0


def write_csv(path, datasets):
    columns = ["店铺", "统计窗口", "开始日期", "结束日期", "商品选项ID", "商品", "后台销量", "后台GMV", "币种", "订单数", "访客UV", "浏览PV", "点击", "搜索", "转化指标", "模式", "缺货标记", "抓取时间", "本次更新", "数据完整"]
    rows = []
    for dataset in datasets:
        for window_name, window in dataset.get("sales_windows", {}).items():
            for row in window.get("rows", []):
                traffic = row.get("traffic") or {}
                rows.append([
                    dataset["store"], window_name, window["start_date"], window["end_date"], row["sku"], row.get("name", ""),
                    row["units"], row["amount"], dataset["currency"], traffic.get("totalOrders"), traffic.get("totalUniqueVisitor"),
                    traffic.get("totalPageViews"), traffic.get("srpClick"), traffic.get("searchVolume"), traffic.get("pvToOrder"),
                    row.get("registration_type"), row.get("is_oos"), window.get("captured_at", dataset.get("observed_at")),
                    dataset.get("refresh_status", "success"), bool(window.get("complete")),
                ])
    temp = path.with_suffix(".csv.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows([[safe_cell(cell) for cell in row] for row in rows])
    os.replace(temp, path)
    return len(rows)


def workbook_runtime(config):
    deps = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
    node = Path(config.get("node_path") or deps / "node/bin/node.exe")
    modules = Path(config.get("node_modules_path") or deps / "node/node_modules")
    if not node.is_file() or not (modules / "@oai/artifact-tool").is_dir():
        return None
    return node, modules


def build_workbook(config, output, render=False):
    runtime = workbook_runtime(config)
    if runtime is None:
        return {"status": "unavailable", "reason": "未找到 Codex Artifact Tool；CSV 已生成，Excel 未更新"}
    node, modules = runtime
    # Copy the one maintained builder to a private runtime directory. The local
    # junction resolves only pre-existing Codex dependencies, never npm downloads.
    work = output / ".workbook-runtime"
    work.mkdir(exist_ok=True)
    link = work / "node_modules"
    if not link.exists():
        if os.name == "nt":
            quoted_link = "'" + str(link).replace("'", "''") + "'"
            quoted_modules = "'" + str(modules).replace("'", "''") + "'"
            command = f"$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path {quoted_link} -Target {quoted_modules} | Out-Null"
            subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True, capture_output=True, timeout=30)
        else:
            link.symlink_to(modules, target_is_directory=True)
    builder = work / "build_workbook.mjs"
    shutil.copyfile(SCRIPTS / "build_workbook.mjs", builder)
    command = [str(node), str(builder), str(output / "workbook-data.json"), str(output / "coupang-data.xlsx")]
    if render:
        command.append("--render")
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "failed", "reason": "Excel 导出执行失败；保留原表及最新 CSV"}
    if result.returncode:
        return {"status": "failed", "reason": "Excel 导出失败；保留原表及最新 CSV"}
    return {"status": "updated"}


def notify(config, output, state):
    from dingtalk_notify import send_work_notice, verify_delivery
    config = notice_config(config)
    if config.get("status") != "configured":
        return {"status": "missing_config", "missing": config.get("missing", [])}
    lines = ["Coupang 四店运营更新", "更新时间：" + state["updated_at"]]
    try:
        summaries = load_json(output / "summary.json").get("stores", [])
    except (OSError, ValueError):
        summaries = []
    labels = {"success": "更新成功", "auth_required": "需要重新登录或完成访问验证", "fetch_failed": "取数失败", "analysis_failed": "分析校验失败"}
    for store in state["stores"]:
        lines.append(f"{store['store']}：{labels.get(store['status'], store['status'])}；{store.get('message', '')}")
        summary = next((item for item in summaries if item["store"] == store["store"]), None)
        if summary and summary.get("refresh_status") == "success" and store.get("fetch_status", store["status"]) == "success":
            denominator = summary["ranking"]["denominator"]
            if denominator.get("amount") is not None:
                lines.append(f"最近完整30天后台销量 {denominator['units']} 件，GMV {denominator['amount']} {summary['currency']}。")
    database = state.get("database", {"status": "not_requested"})
    lines += [f"MySQL 状态：{database['status']}；已提交 {database.get('rows_written', 0)} 条 SKU 或店铺状态记录。",
              f"本机数据记录数：{state.get('row_count', 0)}。", "Excel 状态：" + state.get("workbook", {}).get("status", "unknown"),
              "库存、上架日期或退货明细不足时，相关结论为缺少数据。"]
    # No credentials, customer details, or full exports are sent in notices.
    accepted = send_work_notice(config, "Coupang 四店每半小时更新", "\n".join(lines))
    if accepted.get("status") == "accepted":
        try:
            delivered = verify_delivery(config, accepted["task_id"])
        except Exception:
            # Submission already happened: retain its identity for later polling,
            # even when verification fails unexpectedly. Never submit it again.
            delivered = {"status": "unknown", "error": "verification_failed"}
        return {**accepted, "delivery": delivered}
    return accepted


def save_database(args, config, datasets, state):
    if not (getattr(args, "save_db", False) or config.get("mysql_enabled") is True):
        return {"status": "not_requested"}
    return persist_refresh(config.get("dingtalk_erp_path", ""), datasets, state["stores"], state["updated_at"])


def perform_refresh(args, config, datasets=None, extras_by_store=None):
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    state = {"updated_at": datetime.now(KST).isoformat(timespec="seconds"), "stores": [], "notification": {"status": "not_requested"}}
    with run_lock(output):
        if shutil.disk_usage(output).free < 64 * 1024 * 1024:
            # An authorized scheduled run can still record and notify a failure
            # in the remote ERP without fetching or overwriting local snapshots.
            if datasets is None and (args.notify or getattr(args, "save_db", False) or config.get("mysql_enabled") is True):
                stores = config.get("stores", [])
                if (not isinstance(stores, list) or not stores or len(stores) > 4
                        or any(not isinstance(item, dict) or not isinstance(item.get("store"), str)
                               or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["store"]) for item in stores)
                        or len({item["store"] for item in stores}) != len(stores)):
                    raise ValueError("店铺配置无效")
                state["stores"] = [{"store": item["store"], "status": "fetch_failed", "fetch_status": "fetch_failed",
                                    "message": "本机磁盘空间不足 64MB，本次未取数，保留上次成功快照"} for item in stores]
                state["database"] = save_database(args, config, [], state)
                state["workbook"] = {"status": "not_updated", "reason": "本机磁盘空间不足"}
                if args.notify:
                    try:
                        state["notification"] = notify(config, output, state)
                    except Exception:
                        state["notification"] = {"status": "failed", "reason": "钉钉通知失败"}
                try:
                    atomic_json(output / "status.json", state)
                except OSError:
                    state["local_status"] = "not_saved"
                print(json.dumps(state, ensure_ascii=False))
                return 2
            raise ValueError("磁盘剩余空间不足 64MB，本次更新停止")
        current = []
        if datasets is not None:
            for dataset in datasets:
                dataset["refresh_status"] = "synthetic_demo"
                current.append(dataset)
                state["stores"].append({"store": dataset["store"], "status": "synthetic_demo", "message": "模拟数据"})
        else:
            stores = config.get("stores", [])
            if (not isinstance(stores, list) or not stores
                    or any(not isinstance(item, dict) or not isinstance(item.get("store"), str)
                           or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item["store"]) for item in stores)
                    or len({item["store"] for item in stores}) != len(stores)):
                raise ValueError("店铺配置为空或存在重复店铺")
            for store_config in stores:
                filename = cache_path(output, store_config["store"])
                try:
                    dataset = fetch_store(store_config, args.analysis_date, request_interval=config.get("request_interval", .5), max_pages=config.get("max_pages", 200), progress=lambda event: atomic_json(output / "progress.json", event), network_retries=config.get("network_retries", 2))
                    dataset["refresh_status"] = "success"
                    atomic_json(filename, dataset)
                    current.append(dataset)
                    state["stores"].append({"store": dataset["store"], "status": "success", "fetch_status": "success", "message": "三段日期及全部分页完成"})
                except WingError as error:
                    status = "auth_required" if isinstance(error, AuthError) else "fetch_failed"
                    entry = {"store": store_config["store"], "status": status, "fetch_status": status, "message": str(error)}
                    state["stores"].append(entry)
                    if filename.exists():
                        try:
                            old = cached_dataset(filename, store_config["store"])
                            current.append(old)
                            entry["cache_status"] = "stale"
                        except (OSError, ValueError, TypeError, UnicodeError):
                            entry["cache_status"] = "invalid"
                            entry["message"] += "；旧快照损坏或覆盖无效，未使用旧数据"
        summaries = []
        extras_by_store = extras_by_store or {}
        entries = {entry["store"]: entry for entry in state["stores"]}
        for dataset in current:
            entry = entries[dataset["store"]]
            try:
                extras = None if dataset["refresh_status"] == "stale" else extras_by_store.get(dataset["store"])
                if dataset["refresh_status"] != "stale" and extras is None and args.extras_dir:
                    candidate = extras_path(args.extras_dir, dataset["store"])
                    if candidate.is_file():
                        extras = load_json(candidate)
                summary = analyze_store(dataset, extras, config.get("thresholds"))
                summary["refresh_status"] = dataset["refresh_status"]
                if dataset["refresh_status"] == "stale":
                    summary = stale_summary(summary)
                summaries.append(summary)
                entry["analysis_status"] = "historical" if dataset["refresh_status"] == "stale" else "success"
            except (OSError, ValueError, TypeError, UnicodeError):
                entry["analysis_status"] = "failed"
                if entry["status"] in {"success", "synthetic_demo"}:
                    entry["status"] = "analysis_failed"
                entry["message"] += "；附加数据、数据口径或覆盖校验失败，未生成确定结论"
        atomic_json(output / "datasets.json", {"datasets": current})
        atomic_json(output / "summary.json", {"stores": summaries})
        state["database"] = save_database(args, config, current, state) if datasets is None else {"status": "not_requested"}
        rows = write_csv(output / "coupang-data.csv", current)
        atomic_json(output / "workbook-data.json", {"state": state, "datasets": current, "summaries": summaries})
        body = render_report(summaries) if summaries else "# Coupang 运营分析\n\n本次没有成功取得完整店铺数据，五项问题暂时无法判断。\n"
        prefix = "\n".join(["# 本次更新状态", "", "更新时间：" + state["updated_at"], ""] + [f"- {entry['store']}：{entry['status']}；{entry.get('message','')}" for entry in state["stores"]])
        if any(dataset.get("refresh_status") == "stale" for dataset in current):
            prefix += "\n\n部分表格保留上次成功数据，已标为 stale，不能代表本次更新。"
        (output / "report.md").write_text(prefix + "\n\n" + body, encoding="utf-8")
        state["row_count"] = rows
        try:
            state["workbook"] = ({"status": "not_requested"} if getattr(args, "skip_workbook", False)
                                 else build_workbook(config, output, args.render))
        except (OSError, subprocess.SubprocessError):
            state["workbook"] = {"status": "failed", "reason": "Excel 执行准备失败；已保留最新 CSV"}
        if args.notify:
            try:
                state["notification"] = notify(config, output, state)
            except Exception:
                state["notification"] = {"status": "failed", "reason": "钉钉通知失败；请检查本机应用及接收人配置"}
        atomic_json(output / "status.json", state)
        history_path = output / "refresh-history.json"
        try:
            history = load_json(history_path) if history_path.exists() else []
            if not isinstance(history, list):
                raise ValueError("历史记录格式无效")
        except (OSError, ValueError, UnicodeError):
            history = []
            state["history_status"] = "reset_invalid_history"
            atomic_json(output / "status.json", state)
        atomic_json(history_path, (history + [state])[-96:])
        atomic_json(output / "workbook-data.json", {"state": state, "datasets": current, "summaries": summaries})
    print(json.dumps(state, ensure_ascii=False))
    stores_ok = all(entry["status"] in {"success", "synthetic_demo"} for entry in state["stores"])
    notice_ok = not args.notify or notification_complete(state["notification"])
    database_ok = state["database"]["status"] in {"updated", "not_requested"}
    return 0 if stores_ok and notice_ok and database_ok else 2


def main():
    parser = argparse.ArgumentParser(description="Coupang 只读采集、四店报告、数据表及钉钉工作通知")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--capture", required=True)
    init.add_argument("--config", default="private/coupang-config.json")
    init.add_argument("--erp-config", default="")
    init.add_argument("--replace", action="store_true")
    resolve = commands.add_parser("resolve-recipient")
    resolve.add_argument("--config", default="private/coupang-config.json")
    for name in ("run", "demo"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="private/coupang-config.json")
        command.add_argument("--output-dir", default="outputs/live" if name == "run" else "outputs/v2-demo")
        command.add_argument("--analysis-date")
        command.add_argument("--extras-dir", default="private/extras")
        command.add_argument("--notify", action="store_true")
        command.add_argument("--render", action="store_true")
        command.add_argument("--save-db", action="store_true", help="保存至 ERP MySQL 的 Coupang 新表")
        command.add_argument("--skip-workbook", action="store_true", help="省略可选 Excel 导出")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args)
            return 0
        if args.command == "resolve-recipient":
            return resolve_recipient(args)
        if args.command == "demo":
            if args.notify or args.save_db:
                raise ValueError("模拟模式不会发送真实通知或写入 ERP 数据库")
            from demo_operations import build_demo_bundle
            bundle = build_demo_bundle(args.analysis_date or "2026-10-12")
            return perform_refresh(args, {}, bundle["datasets"], bundle["extras"])
        return perform_refresh(args, load_json(args.config))
    except (ValueError, OSError, json.JSONDecodeError, WingError):
        print(json.dumps({"status": "configuration_or_io_error", "message": "配置、文件或执行条件不满足；没有输出凭据，请检查本机配置及磁盘空间"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
