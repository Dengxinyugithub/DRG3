#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHS-DRG 3.0 本地分组服务（HTTP / JSON，纯标准库，无任何外网请求）。

启动：
    python app.py                 # 监听 127.0.0.1:8765
    python app.py --port 9000     # 换端口
    python app.py --rebuild       # 强制重新解析 xlsx（配置更新后使用）

接口一览：
    GET  /                              浏览器测试页面
    GET  /api/v1/health                 健康检查
    GET  /api/v1/version                版本与配置摘要
    GET  /api/v1/config/summary         配置统计
    GET  /api/v1/codes/search           诊断/手术编码检索（联想输入）
    GET  /api/v1/template/csv           CSV 批量模板
    POST /api/v1/group                  单条病历分组
    POST /api/v1/group/batch            批量分组（JSON 数组）
    POST /api/v1/group/csv              批量分组（CSV 文本）
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from grouper import ConfigError, DrgGrouper, load_config  # noqa: E402

APP_VERSION = "1.1.1"
MAX_BODY_BYTES = 64 * 1024 * 1024  # 64 MB
MAX_BATCH_RECORDS = 50000

CSV_COLUMNS = [
    ("patient_id", ["patient_id", "病例号", "患者id", "患者编号", "住院号"]),
    ("age", ["age", "年龄"]),
    ("age_unit", ["age_unit", "年龄单位"]),
    ("gender", ["gender", "性别"]),
    ("birth_weight_g", ["birth_weight_g", "birth_weight", "出生体重", "出生体重g", "新生儿体重"]),
    ("primary_diagnosis_code", ["primary_diagnosis_code", "主要诊断编码", "主诊断编码"]),
    ("primary_diagnosis_name", ["primary_diagnosis_name", "主要诊断名称", "主诊断名称"]),
    ("other_diagnosis_codes", ["other_diagnosis_codes", "其他诊断编码"]),
    ("other_diagnosis_names", ["other_diagnosis_names", "其他诊断名称"]),
    ("primary_operation_code", ["primary_operation_code", "主要手术编码", "主手术编码", "主要操作编码"]),
    ("primary_operation_name", ["primary_operation_name", "主要手术名称", "主手术名称", "主要操作名称"]),
    ("other_operation_codes", ["other_operation_codes", "其他手术编码", "其他操作编码"]),
    ("other_operation_names", ["other_operation_names", "其他手术名称", "其他操作名称"]),
]

CSV_OUT_COLUMNS = [
    "patient_id",
    "is_grouped",
    "mdc_code",
    "mdc_name",
    "adrg_code",
    "adrg_name",
    "drg_code",
    "drg_name",
    "rw",
    "reason_code",
    "reason_message",
]

_CSV_HEADER_ALIASES: Dict[str, str] = {}
for canonical, aliases in CSV_COLUMNS:
    for alias in aliases:
        _CSV_HEADER_ALIASES[alias.strip().lower()] = canonical


def _attachment_header(filename: str, fallback: str = "download.csv") -> Dict[str, str]:
    """HTTP 头只能是 latin-1，中文文件名用 RFC 5987 的 filename* 传递。"""
    from urllib.parse import quote

    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or fallback
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )
    }


# --------------------------------------------------------------------------- #
# 请求体解析
# --------------------------------------------------------------------------- #

class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _decode_body(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"')
    for candidate in (charset, "utf-8-sig", "gb18030"):
        try:
            text = raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
        # Excel 另存为「CSV UTF-8」会带 BOM，去掉以免污染首个表头
        return text[1:] if text[:1] == "\ufeff" else text
    raise RequestError(400, "BAD_ENCODING", "请求体编码无法识别，请使用 UTF-8")


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RequestError(
            400, "BAD_JSON", f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc


def _split_codes(value: str) -> List[str]:
    if not value:
        return []
    for sep in ("|", "、", "，"):
        value = value.replace(sep, ";")
    return [item.strip() for item in value.split(";") if item.strip()]


def _row_to_record(row: Dict[str, str]) -> Dict[str, Any]:
    data: Dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        canonical = _CSV_HEADER_ALIASES.get(str(key).strip().lower())
        if canonical:
            data[canonical] = (value or "").strip()

    dx_codes = _split_codes(data.get("other_diagnosis_codes", ""))
    dx_names = _split_codes(data.get("other_diagnosis_names", ""))
    op_codes = _split_codes(data.get("other_operation_codes", ""))
    op_names = _split_codes(data.get("other_operation_names", ""))

    record: Dict[str, Any] = {
        "patient_id": data.get("patient_id") or None,
        "age": data.get("age") or None,
        "age_unit": data.get("age_unit") or "years",
        "gender": data.get("gender") or None,
        "primary_diagnosis": {
            "code": data.get("primary_diagnosis_code", ""),
            "name": data.get("primary_diagnosis_name", ""),
        },
        "other_diagnoses": [
            {"code": code, "name": dx_names[i] if i < len(dx_names) else ""}
            for i, code in enumerate(dx_codes)
        ],
        "primary_operation": (
            {
                "code": data.get("primary_operation_code", ""),
                "name": data.get("primary_operation_name", ""),
            }
            if data.get("primary_operation_code")
            else None
        ),
        "other_operations": [
            {"code": code, "name": op_names[i] if i < len(op_names) else ""}
            for i, code in enumerate(op_codes)
        ],
    }
    weight = data.get("birth_weight_g")
    if weight:
        try:
            record["birth_weight_g"] = int(float(weight))
        except ValueError:
            record["birth_weight_g"] = weight
    return record


def _result_to_row(result: Dict[str, Any]) -> Dict[str, str]:
    grouping = result.get("grouping_result") or {}
    mdc = grouping.get("mdc") or {}
    adrg = grouping.get("adrg") or {}
    drg = grouping.get("drg") or {}
    reason = result.get("reason") or {}
    return {
        "patient_id": grouping.get("patient_id") or "",
        "is_grouped": "true" if result.get("is_grouped") else "false",
        "mdc_code": mdc.get("code") or "",
        "mdc_name": mdc.get("name") or "",
        "adrg_code": adrg.get("code") or "",
        "adrg_name": adrg.get("name") or "",
        "drg_code": drg.get("code") or "",
        "drg_name": drg.get("name") or "",
        "rw": grouping.get("rw") or "NAN",
        "reason_code": reason.get("code") or "",
        "reason_message": reason.get("message") or "",
    }


# --------------------------------------------------------------------------- #
# HTTP 处理器
# --------------------------------------------------------------------------- #

class DrgHandler(BaseHTTPRequestHandler):
    server_version = f"DRG3Grouper/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    # 由 create_server 注入
    grouper: DrgGrouper
    web_dir: Path
    started_at: float
    quiet: bool = False

    # ------------------------------ 工具方法 ------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.quiet:
            return
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, pretty: bool = False) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            default=str,
        ).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str, detail: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
        if detail:
            payload["error"]["detail"] = detail
        self._json(status, payload, pretty=True)

    def _read_body(self) -> bytes:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return b""
        try:
            length = int(length_header)
        except ValueError:
            raise RequestError(400, "BAD_LENGTH", "Content-Length 非法")
        if length < 0 or length > MAX_BODY_BYTES:
            raise RequestError(413, "BODY_TOO_LARGE", f"请求体超过 {MAX_BODY_BYTES // 1024 // 1024} MB 限制")
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # ------------------------------ 路由 ------------------------------ #
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                self._serve_file("index.html")
            elif path in ("/health", "/api/v1/health"):
                self._json(
                    200,
                    {
                        "ok": True,
                        "status": "healthy",
                        "app": "CHS-DRG 3.0 本地分组服务",
                        "version": APP_VERSION,
                        "uptime_seconds": round(time.time() - self.started_at, 1),
                        "stats": self.grouper.stats,
                    },
                    pretty=True,
                )
            elif path in ("/version", "/api/v1/version"):
                self._json(
                    200,
                    {
                        "ok": True,
                        "app_version": APP_VERSION,
                        "config": self.grouper.config.summary(),
                        "python": sys.version.split()[0],
                    },
                    pretty=True,
                )
            elif path == "/api/v1/config/summary":
                self._json(200, {"ok": True, "data": self.grouper.config.summary()}, pretty=True)
            elif path == "/api/v1/codes/search":
                self._handle_search(query)
            elif path == "/api/v1/template/csv":
                self._handle_template()
            elif path.startswith("/web/"):
                self._serve_file(path[len("/web/"):])
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._error(404, "NOT_FOUND", f"未知路径 {path}")
        except RequestError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception:  # pragma: no cover
            self._error(500, "INTERNAL_ERROR", "服务内部错误", traceback.format_exc(limit=5))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            raw = self._read_body()
            content_type = self.headers.get("Content-Type", "")
            if path == "/api/v1/group":
                self._handle_group(raw, content_type, query)
            elif path == "/api/v1/group/batch":
                self._handle_batch(raw, content_type, query)
            elif path == "/api/v1/group/csv":
                self._handle_csv(raw, content_type, query)
            else:
                self._error(404, "NOT_FOUND", f"未知路径 {path}")
        except RequestError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception:  # pragma: no cover
            self._error(500, "INTERNAL_ERROR", "服务内部错误", traceback.format_exc(limit=5))

    # ------------------------------ 端点实现 ------------------------------ #
    def _handle_search(self, query: Dict[str, List[str]]) -> None:
        kind = (query.get("type", ["dx"])[0] or "dx").lower()
        text = query.get("q", [""])[0]
        try:
            limit = int(query.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        items = self.grouper.config.search_codes(kind, text, limit)
        self._json(200, {"ok": True, "count": len(items), "items": items}, pretty=True)

    def _handle_template(self) -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([name for name, _ in CSV_COLUMNS])
        writer.writerow(
            [
                "345899002",
                "65",
                "years",
                "M",
                "",
                "I63.900",
                "脑梗死",
                "E11.900;I10.000",
                "2型糖尿病;高血压病",
                "38.1201",
                "颈动脉内膜剥脱术",
                "88.4100;96.7201",
                "脑血管造影;有创呼吸机支持治疗",
            ]
        )
        body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        self._send(
            200,
            body,
            "text/csv; charset=utf-8",
            _attachment_header("DRG批量分组模板.csv", "DRG_template.csv"),
        )

    def _handle_group(self, raw: bytes, content_type: str, query: Dict[str, List[str]]) -> None:
        text = _decode_body(raw, content_type)
        payload = _parse_json(text)
        if not isinstance(payload, dict):
            raise RequestError(400, "BAD_REQUEST", "单条分组接口要求请求体是 JSON 对象")
        debug = str(query.get("debug", ["0"])[0]).lower() in ("1", "true", "yes") or bool(payload.get("debug"))
        result = self.grouper.group_record(payload, debug=debug)
        self._json(200, result, pretty=True)

    def _handle_batch(self, raw: bytes, content_type: str, query: Dict[str, List[str]]) -> None:
        text = _decode_body(raw, content_type)
        payload = _parse_json(text)
        debug = str(query.get("debug", ["0"])[0]).lower() in ("1", "true", "yes")
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            records = payload["records"]
            debug = debug or bool(payload.get("debug"))
        elif isinstance(payload, list):
            records = payload
        else:
            raise RequestError(
                400,
                "BAD_REQUEST",
                '批量分组要求请求体是 JSON 数组，或 {"records": [...]} 对象',
            )
        if len(records) > MAX_BATCH_RECORDS:
            raise RequestError(
                413, "TOO_MANY_RECORDS", f"单次批量最多 {MAX_BATCH_RECORDS} 条，收到 {len(records)} 条"
            )
        result = self.grouper.group_batch(records, debug=debug)
        self._json(200, {"ok": True, **result})

    def _handle_csv(self, raw: bytes, content_type: str, query: Dict[str, List[str]]) -> None:
        text = _decode_body(raw, content_type)
        if not text.strip():
            raise RequestError(400, "EMPTY_CSV", "CSV 内容为空")
        # 容忍文件开头的空行（Excel 另存、复制粘贴常见）
        lines = text.splitlines()
        start = 0
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start >= len(lines):
            raise RequestError(400, "EMPTY_CSV", "CSV 内容为空")
        text = "\n".join(lines[start:])
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise RequestError(400, "BAD_CSV", "CSV 缺少表头行")
        # 清理表头里的 BOM / 空白，避免 "﻿patient_id" 这类键匹配不上
        cleaned = [(name or "").strip().lstrip("\ufeff") for name in reader.fieldnames]
        if cleaned != reader.fieldnames:
            reader.fieldnames = cleaned
        records = []
        for row in reader:
            if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
                continue
            records.append(_row_to_record(row))
        if len(records) > MAX_BATCH_RECORDS:
            raise RequestError(
                413, "TOO_MANY_RECORDS", f"单次批量最多 {MAX_BATCH_RECORDS} 条，收到 {len(records)} 条"
            )
        if not records:
            raise RequestError(400, "EMPTY_CSV", "CSV 中没有数据行（只有表头或全部为空）")
        debug = str(query.get("debug", ["0"])[0]).lower() in ("1", "true", "yes")
        result = self.grouper.group_batch(records, debug=debug)
        output = (query.get("output", ["json"])[0] or "json").lower()
        if output == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=CSV_OUT_COLUMNS)
            writer.writeheader()
            for item in result["results"]:
                writer.writerow(_result_to_row(item))
            body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
            self._send(
                200,
                body,
                "text/csv; charset=utf-8",
                _attachment_header("DRG分组结果.csv", "DRG_grouping_result.csv"),
            )
            return
        self._json(200, {"ok": True, **result})

    # ------------------------------ 静态文件 ------------------------------ #
    def _serve_file(self, relative: str) -> None:
        target = (self.web_dir / relative).resolve()
        if not str(target).startswith(str(self.web_dir.resolve())) or not target.is_file():
            self._error(404, "NOT_FOUND", f"文件不存在：{relative}")
            return
        content_type = "text/html; charset=utf-8"
        if target.suffix.lower() == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix.lower() == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif target.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
            content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".svg": "image/svg+xml",
            }[target.suffix.lower()]
        self._send(200, target.read_bytes(), content_type)


class GrouperHTTPServer(ThreadingHTTPServer):
    """多线程服务；客户端提前断开连接时不再打印无意义的堆栈。

    ``allow_reuse_address`` 在 Windows 上会让两个进程同时绑定同一端口（SO_REUSEADDR
    语义不同），表现为"旧实例还在应答、新实例看着也启动成功"。因此在 Windows 上关掉它，
    端口被占用时直接报错，配合 ``_probe_existing_instance`` 给出可读提示。
    """

    daemon_threads = True
    allow_reuse_address = platform.system() != "Windows"

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def create_server(host: str, port: int, grouper: DrgGrouper, quiet: bool = False) -> GrouperHTTPServer:
    handler = type(
        "BoundDrgHandler",
        (DrgHandler,),
        {
            "grouper": grouper,
            "web_dir": BASE_DIR / "web",
            "started_at": time.time(),
            "quiet": quiet,
        },
    )
    return GrouperHTTPServer((host, port), handler)


def _probe_existing_instance(host: str, port: int) -> Optional[Dict[str, Any]]:
    """端口被占用时，探测占用者是不是另一个分组器实例（只访问本机回环）。"""
    import json
    import urllib.request

    target = "127.0.0.1" if host in ("", "0.0.0.0", "::", "::0") else host
    try:
        with urllib.request.urlopen(f"http://{target}:{port}/api/v1/health", timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) and payload.get("app") else None


def _configure_stdio() -> None:
    """Windows 控制台非 UTF-8 时，中文输出会抛 UnicodeEncodeError；其它平台不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass


def _install_sigterm(server: "GrouperHTTPServer") -> None:
    """支持 systemd / docker stop / kill 的优雅退出（Windows 上注册无效则忽略）。"""
    import signal
    import threading

    def _stop(signum: int, frame: Any) -> None:  # noqa: ARG001
        print("\n收到终止信号，正在关闭服务…")
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError, OSError):  # pragma: no cover
        pass


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(description="CHS-DRG 3.0 本地分组服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机可访问）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--config", default=None, help="分组方案 xlsx 路径")
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存，重新解析 xlsx")
    parser.add_argument("--quiet", action="store_true", help="不打印访问日志")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器测试页面")
    parser.add_argument("--check", action="store_true", help="只加载并校验配置，打印规模后退出")
    args = parser.parse_args(argv)

    try:
        config = load_config(config_path=Path(args.config) if args.config else None, rebuild=args.rebuild)
    except ConfigError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    if args.check:
        summary = config.summary()
        print("=" * 72)
        print("  配置校验通过")
        print("=" * 72)
        for key, value in summary.items():
            print(f"  {key:20s}: {value}")
        print("=" * 72)
        return 0

    grouper = DrgGrouper(config)
    try:
        server = create_server(args.host, args.port, grouper, quiet=args.quiet)
    except OSError as exc:
        print(f"[错误] 无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
        existing = _probe_existing_instance(args.host, args.port)
        if existing:
            print(
                f"       该端口已被另一个分组器实例占用（版本 {existing.get('version')}，"
                f"已处理 {existing.get('stats', {}).get('total', 0)} 条）。",
                file=sys.stderr,
            )
            print("       请先关闭那个实例，或用 --port 换一个端口（例如 --port 8766）。", file=sys.stderr)
        else:
            print("       端口被其他程序占用，可用 --port 换一个端口，例如：--port 8766", file=sys.stderr)
        return 3
    summary = config.summary()

    print("=" * 72)
    print("  CHS-DRG 3.0 本地分组服务")
    print("=" * 72)
    print(f"  版本        : {APP_VERSION}")
    print(f"  运行环境    : {platform.system()} {platform.release()} / Python {platform.python_version()}")
    print(f"  配置文件    : {summary['source']}")
    print(f"  配置规模    : MDC {summary['mdc']} / ADRG {summary['adrg']} / DRG {summary['drg']}")
    print(f"                CC/MCC {summary['cc_mcc']} 条，排除表 {summary['exclusion_tables']} 张")
    print(f"  监听地址    : http://{args.host}:{args.port}")
    print(f"  测试页面    : http://{args.host}:{args.port}/")
    print("  接口        : POST /api/v1/group        单条分组")
    print("                POST /api/v1/group/batch  批量分组（JSON）")
    print("                POST /api/v1/group/csv    批量分组（CSV）")
    print("  说明        : 所有计算在本机完成，不会访问外网")
    print("  停止服务    : 按 Ctrl+C（或 kill / systemctl stop / docker stop）")
    print("=" * 72)
    sys.stdout.flush()

    if args.open:
        import threading
        import webbrowser

        url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    _install_sigterm(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
