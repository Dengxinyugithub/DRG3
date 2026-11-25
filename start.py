#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台启动器（Windows / macOS / Linux 通用）。

用法：
    python start.py                 # 默认 127.0.0.1:8765，并打开浏览器
    python start.py --no-open       # 不打开浏览器（服务器 / 无桌面环境）
    python start.py --port 9000
    python start.py --check         # 只校验配置并打印规模，不启动服务
    python start.py --rebuild       # 忽略缓存，重新解析 xlsx

各平台的一行启动脚本（start.bat / start.sh / start.command）都只是转调本文件，
平台相关处理集中在这里，避免多份脚本行为不一致。
"""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

CONFIG_FILE = BASE_DIR / "data" / "DRG3.0分组方案配置信息.xlsx"
CACHE_FILE = BASE_DIR / "data" / "config.cache.json.gz"

IS_WINDOWS = platform.system() == "Windows"


def _configure_stdio() -> None:
    """Windows 控制台默认不是 UTF-8，中文横幅会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - 老版本 Python / 被重定向
            pass


def _platform_line() -> str:
    return (
        f"{platform.system()} {platform.release()} / "
        f"Python {platform.python_version()} / {platform.machine()}"
    )


def _pip_hint() -> str:
    if IS_WINDOWS:
        return "py -m pip install openpyxl   或   python -m pip install openpyxl"
    return "python3 -m pip install openpyxl"


def _has_openpyxl() -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()

    parser = argparse.ArgumentParser(
        description="CHS-DRG 3.0 本地分组器启动器（Windows / macOS / Linux）"
    )
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--check", action="store_true", help="只校验配置并打印规模，不启动服务")
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存，重新解析 xlsx")
    parser.add_argument("--quiet", action="store_true", help="不打印访问日志")
    args = parser.parse_args(argv)

    print("=" * 72)
    print("  CHS-DRG 3.0 本地分组器")
    print(f"  运行环境：{_platform_line()}")
    print("=" * 72)

    if not CONFIG_FILE.exists():
        print(f"[错误] 找不到配置：{CONFIG_FILE}", file=sys.stderr)
        print("       请把《按病组（DRG）付费3.0版分组方案配置信息.xlsx》放到 data/ 目录。", file=sys.stderr)
        return 2

    # 有缓存时不需要 openpyxl；没有缓存才需要现场解析 xlsx
    if not CACHE_FILE.exists() and not _has_openpyxl():
        print("[错误] 首次启动需要 openpyxl 解析 xlsx，当前环境没有安装。", file=sys.stderr)
        print(f"       安装命令：{_pip_hint()}", file=sys.stderr)
        print("       安装后再执行本脚本；缓存生成后可不再依赖 openpyxl。", file=sys.stderr)
        return 2

    app_argv = ["--port", str(args.port), "--host", args.host]
    if args.rebuild:
        app_argv.append("--rebuild")
    if args.quiet:
        app_argv.append("--quiet")
    if args.check:
        app_argv.append("--check")
    elif not args.no_open:
        app_argv.append("--open")

    from app import main as app_main  # 延迟导入：--check / 报错路径不必加载服务

    return app_main(app_argv)


if __name__ == "__main__":
    raise SystemExit(main())
