# -*- coding: utf-8 -*-
"""加载国家医保局《按病组（DRG）付费3.0版分组方案配置信息》工作簿。

工作簿 6 张表：
    MDC    主要诊断大类（含 MDC 规则、排序）
    ADRG   核心病组（含 ADRG 规则、所属 MDC、MDC 内排序）
    DRG    细分组（含 DRG 规则、所属 ADRG/MDC、排序）
    集合    分组规则引用的编码集合（DI_/OP_ 前缀，约 10.7 万条）
    CC     严重/一般合并症与并发症（MCC/CC），含排除表编号
    排除表  每个 CC/MCC 对应的"主要诊断排除"清单（EX_1..EX_n）

首次启动解析 xlsx（约数秒），随后写入 data/config.cache.json.gz，
再次启动直接读缓存（不到 1 秒）。xlsx 变更（大小/修改时间变化）会自动重建。
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .rules import (
    RuleSyntaxError,
    normalize_code,
    parse_rule,
    referenced_identifiers,
)

CACHE_VERSION = 2

# 规则里允许出现的字段（其余标识符必须是集合名、MCC 或 CC）
RULE_FIELDS = {"ZYZD", "QTZD", "ZYSS", "QTSS", "XB", "NL", "XSRTL", "XSRTZ"}

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "data" / "DRG3.0分组方案配置信息.xlsx"
DEFAULT_CACHE = BASE_DIR / "data" / "config.cache.json.gz"


class ConfigError(RuntimeError):
    """配置缺失或损坏。"""


class GrouperConfig:
    """内存中的分组配置。"""

    def __init__(
        self,
        source_path: Path,
        mdc_rows: List[Dict[str, Any]],
        adrg_rows: List[Dict[str, Any]],
        drg_rows: List[Dict[str, Any]],
        sets: Dict[str, Set[str]],
        exclusions: Dict[str, Set[str]],
        cc_rows: List[Dict[str, Any]],
        dx_names: Dict[str, str],
        op_names: Dict[str, str],
    ) -> None:
        self.source_path = source_path
        self.loaded_at = time.strftime("%Y-%m-%d %H:%M:%S")

        self.mdc_rows = mdc_rows
        self.adrg_rows = adrg_rows
        self.drg_rows = drg_rows
        self.sets = {name: frozenset(codes) for name, codes in sets.items()}
        self.exclusions = {name: frozenset(codes) for name, codes in exclusions.items()}
        self.dx_names = dx_names
        self.op_names = op_names

        self.mdc_by_code: Dict[str, Dict[str, Any]] = {r["code"]: r for r in mdc_rows}
        self.adrg_by_code: Dict[str, Dict[str, Any]] = {r["code"]: r for r in adrg_rows}
        self.drg_by_code: Dict[str, Dict[str, Any]] = {r["code"]: r for r in drg_rows}

        # 按 MDC / ADRG 分组，并保持配置中的排序
        self.adrg_by_mdc: Dict[str, List[Dict[str, Any]]] = {}
        for row in adrg_rows:
            self.adrg_by_mdc.setdefault(row["mdc"], []).append(row)
        for rows in self.adrg_by_mdc.values():
            rows.sort(key=lambda r: (r["order"], r["seq"]))

        self.drg_by_adrg: Dict[str, List[Dict[str, Any]]] = {}
        for row in drg_rows:
            self.drg_by_adrg.setdefault(row["adrg"], []).append(row)
        for rows in self.drg_by_adrg.values():
            rows.sort(key=lambda r: (r["order"], r["seq"]))

        # CC / MCC
        self.cc_by_code: Dict[str, Dict[str, Any]] = {}
        for row in cc_rows:
            self.cc_by_code[row["code"]] = row

        self._validate()

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def _validate(self) -> None:
        problems: List[str] = []

        for row in self.mdc_rows + self.adrg_rows + self.drg_rows:
            ast = row.get("ast")
            if ast is None:
                continue
            for ident in referenced_identifiers(ast):
                if ident in RULE_FIELDS or ident in ("MCC", "CC"):
                    continue
                if ident not in self.sets:
                    problems.append(f"{row['kind']} {row['code']} 引用了不存在的集合 {ident}")

        # 兜底组（规则为空）必须排在同级最后，否则会吃掉后面的规则
        for mdc_code, rows in self.adrg_by_mdc.items():
            blanks = [i for i, r in enumerate(rows) if r["ast"] is None]
            if blanks and blanks != list(range(len(rows) - len(blanks), len(rows))):
                problems.append(f"MDC {mdc_code} 内存在位置不当的兜底 ADRG 行：{blanks}")

        for adrg_code, rows in self.drg_by_adrg.items():
            blanks = [i for i, r in enumerate(rows) if r["ast"] is None]
            if blanks and blanks != list(range(len(rows) - len(blanks), len(rows))):
                problems.append(f"ADRG {adrg_code} 内存在位置不当的兜底 DRG 行：{blanks}")

        # 每个 CC/MCC 行引用的排除表必须存在（否则排除判定会静默失效）
        for row in self.cc_by_code.values():
            ex_name = row.get("ex") or ""
            if ex_name and ex_name not in self.exclusions:
                problems.append(f"CC {row['code']} 引用了不存在的排除表 {ex_name}")

        if problems:
            raise ConfigError("配置校验失败：\n  " + "\n  ".join(problems[:50]))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def display_name(self, kind: str, code: str, name: str) -> str:
        """兜底组在配置里没有名称，这里补一个可读名称。"""
        if name:
            return name
        if code.endswith("QY"):
            return "歧义组"
        return "未入组"

    def summary(self) -> Dict[str, Any]:
        return {
            "source": str(self.source_path),
            "loaded_at": self.loaded_at,
            "mdc": len(self.mdc_rows),
            "adrg": len(self.adrg_rows),
            "drg": len(self.drg_rows),
            "sets": len(self.sets),
            "set_codes": sum(len(v) for v in self.sets.values()),
            "cc_mcc": len(self.cc_by_code),
            "mcc": sum(1 for r in self.cc_by_code.values() if r["type"] == "MCC"),
            "cc": sum(1 for r in self.cc_by_code.values() if r["type"] == "CC"),
            "exclusion_tables": len(self.exclusions),
            "exclusion_codes": sum(len(v) for v in self.exclusions.values()),
            "diagnosis_names": len(self.dx_names),
            "operation_names": len(self.op_names),
        }

    def search_codes(self, kind: str, query: str, limit: int = 20) -> List[Dict[str, str]]:
        """按编码或名称检索诊断/手术编码（供 HTML 页面联想输入）。"""
        mapping = self.dx_names if kind in ("dx", "diagnosis") else self.op_names
        text = (query or "").strip().lower()
        if not text:
            return []
        exact: List[Tuple[int, str]] = []
        for code, name in mapping.items():
            low_code = code.lower()
            if low_code.startswith(text):
                exact.append((0, code))
            elif text in low_code:
                exact.append((1, code))
            elif text in (name or "").lower():
                exact.append((2 + (0 if (name or "").lower().startswith(text) else 1), code))
        exact.sort(key=lambda item: (item[0], len(item[1]), item[1]))
        return [{"code": code, "name": mapping[code]} for _, code in exact[: max(1, min(limit, 100))]]


# --------------------------------------------------------------------------- #
# 解析 xlsx
# --------------------------------------------------------------------------- #

def _cell(value: Any) -> str:
    """xlsx 单元格 -> 去空白字符串（规则里的换行保留为空格）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    return text.replace("\r\n", "\n").strip()


def _read_xlsx(path: Path) -> Dict[str, Any]:
    try:
        import openpyxl  # 延迟导入，缓存命中时不需要
    except ImportError as exc:  # pragma: no cover
        raise ConfigError(
            "缺少 openpyxl，无法解析 xlsx。请先执行：pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        # ---- MDC（第 1 行是总标题，第 2 行才是表头） ----
        mdc_rows: List[Dict[str, Any]] = []
        it = wb["MDC"].iter_rows(values_only=True)
        next(it, None)
        header = [_cell(c) for c in next(it, [])]
        index = {name: i for i, name in enumerate(header)}
        for seq, raw in enumerate(it):
            code = _cell(raw[index["MDC编码"]])
            if not code:
                continue
            mdc_rows.append(
                {
                    "kind": "MDC",
                    "code": normalize_code(code),
                    "name": _cell(raw[index["MDC名称"]]),
                    "rule": _cell(raw[index["MDC规则"]]),
                    "order": int(_cell(raw[index["排序"]]) or 0),
                    "seq": seq,
                }
            )

        # ---- ADRG ----
        adrg_rows: List[Dict[str, Any]] = []
        it = wb["ADRG"].iter_rows(values_only=True)
        header = [_cell(c) for c in next(it, [])]
        index = {name: i for i, name in enumerate(header)}
        for seq, raw in enumerate(it):
            code = _cell(raw[index["ADRG编码"]])
            if not code:
                continue
            adrg_rows.append(
                {
                    "kind": "ADRG",
                    "code": normalize_code(code),
                    "name": _cell(raw[index["ADRG名称"]]),
                    "rule": _cell(raw[index["ADRG规则"]]),
                    "mdc": normalize_code(_cell(raw[index["所属MDC编码"]])),
                    "order": int(_cell(raw[index["MDC内排序"]]) or 0),
                    "seq": seq,
                }
            )

        # ---- DRG ----
        drg_rows: List[Dict[str, Any]] = []
        it = wb["DRG"].iter_rows(values_only=True)
        header = [_cell(c) for c in next(it, [])]
        index = {name: i for i, name in enumerate(header)}
        for seq, raw in enumerate(it):
            code = _cell(raw[index["DRG"]])
            if not code:
                continue
            drg_rows.append(
                {
                    "kind": "DRG",
                    "code": normalize_code(code),
                    "name": _cell(raw[index["DRG名称"]]),
                    "rule": _cell(raw[index["DRG规则"]]),
                    "adrg": normalize_code(_cell(raw[index["所属ADRG编码"]])),
                    "mdc": normalize_code(_cell(raw[index["所属MDC编码"]])),
                    "order": int(_cell(raw[index["排序"]]) or 0),
                    "seq": seq,
                }
            )

        # ---- 集合 ----
        sets: Dict[str, Set[str]] = {}
        dx_names: Dict[str, str] = {}
        op_names: Dict[str, str] = {}
        it = wb["集合"].iter_rows(values_only=True)
        header = [_cell(c) for c in next(it, [])]
        index = {name: i for i, name in enumerate(header)}
        for raw in it:
            set_name = _cell(raw[index["集合编号"]])
            code = normalize_code(_cell(raw[index["ICD编码"]]))
            if not set_name or not code:
                continue
            sets.setdefault(set_name, set()).add(code)
            name = _cell(raw[index["ICD名称"]])
            kind = _cell(raw[index["类型编号"]])
            target = op_names if kind == "OP" else dx_names
            if code not in target and name:
                target[code] = name

        # ---- CC / MCC ----
        cc_rows: List[Dict[str, Any]] = []
        it = wb["CC"].iter_rows(values_only=True)
        header = [_cell(c) for c in next(it, [])]
        index = {name: i for i, name in enumerate(header)}
        for raw in it:
            code = normalize_code(_cell(raw[index["疾病编码"]]))
            if not code:
                continue
            name = _cell(raw[index["疾病名称"]])
            ex_name = _cell(raw[index["排除表"]])
            if ex_name in ("无", "-", "/", "—"):
                ex_name = ""
            cc_rows.append(
                {
                    "code": code,
                    "name": name,
                    "type": _cell(raw[index["类型"]]).upper(),
                    "ex": ex_name,
                }
            )
            dx_names.setdefault(code, name)

        # ---- 排除表 ----
        exclusions: Dict[str, Set[str]] = {}
        it = wb["排除表"].iter_rows(values_only=True)
        header = [_cell(c) for c in next(it, [])]
        index = {name: i for i, name in enumerate(header)}
        for raw in it:
            set_name = _cell(raw[index["集合编号"]])
            code = normalize_code(_cell(raw[index["ICD编码"]]))
            if set_name and code:
                exclusions.setdefault(set_name, set()).add(code)
    finally:
        wb.close()

    return {
        "cache_version": CACHE_VERSION,
        "source": {"path": str(path), "size": path.stat().st_size, "mtime": int(path.stat().st_mtime)},
        "mdc": mdc_rows,
        "adrg": adrg_rows,
        "drg": drg_rows,
        "sets": {k: sorted(v) for k, v in sets.items()},
        "cc": cc_rows,
        "exclusions": {k: sorted(v) for k, v in exclusions.items()},
        "dx_names": dx_names,
        "op_names": op_names,
    }


def _write_cache(cache_path: Path, payload: Dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, cache_path)


def _read_cache(cache_path: Path, source: Path) -> Optional[Dict[str, Any]]:
    if not cache_path.exists():
        return None
    try:
        with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    if payload.get("cache_version") != CACHE_VERSION:
        return None
    source_info = payload.get("source") or {}
    stat = source.stat()
    if source_info.get("size") != stat.st_size or source_info.get("mtime") != int(stat.st_mtime):
        return None
    return payload


def load_config(
    config_path: Optional[Path] = None,
    cache_path: Optional[Path] = None,
    rebuild: bool = False,
) -> GrouperConfig:
    """加载配置：优先缓存，必要时解析 xlsx 并写缓存。"""
    source = Path(config_path or os.environ.get("DRG30_CONFIG") or DEFAULT_CONFIG)
    if not source.exists():
        raise ConfigError(f"找不到配置文件：{source}")
    cache = Path(cache_path or os.environ.get("DRG30_CACHE") or DEFAULT_CACHE)

    payload = None if rebuild else _read_cache(cache, source)
    from_cache = payload is not None
    if payload is None:
        payload = _read_xlsx(source)
        try:
            _write_cache(cache, payload)
        except OSError:
            pass  # 无写权限时照常运行

    # 解析规则 AST
    for key in ("mdc", "adrg", "drg"):
        for row in payload[key]:
            try:
                row["ast"] = parse_rule(row.get("rule"))
            except RuleSyntaxError as exc:
                raise ConfigError(f"{row['kind']} {row['code']} 规则解析失败：{exc}") from exc

    config = GrouperConfig(
        source_path=source,
        mdc_rows=payload["mdc"],
        adrg_rows=payload["adrg"],
        drg_rows=payload["drg"],
        sets={k: set(v) for k, v in payload["sets"].items()},
        exclusions={k: set(v) for k, v in payload["exclusions"].items()},
        cc_rows=payload["cc"],
        dx_names=payload["dx_names"],
        op_names=payload["op_names"],
    )
    config.loaded_from_cache = from_cache
    return config
