# -*- coding: utf-8 -*-
"""CHS-DRG 3.0 分组引擎。

分组流程（严格按配置表中的排序执行）：

1. 先期分组（MDCA）：AA1..AH3，命中即出组，不再看主诊断。
2. 新生儿（MDCP）、多发严重创伤（MDCZ）等 MDC 按 "排序" 逐条匹配。
3. 主诊断大类（MDCB..MDCY）按主诊表匹配。
4. MDC 内 ADRG：先外科/操作组；若"有主要手术但没进外科组"则落入该 MDC 的
   歧义组（xQY，规则为 ZYSS in OP_ALL）；最后是 x00 兜底（未入组）。
5. ADRG 内 DRG：MCC -> CC -> 不伴合并症（规则为空的行为兜底）。
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import GrouperConfig, RULE_FIELDS
from .rules import evaluate, normalize_code

RW_VALUE = "NAN"

# 记录级错误码
E_INVALID_INPUT = "INVALID_INPUT"
E_MISSING_PRIMARY_DIAGNOSIS = "MISSING_PRIMARY_DIAGNOSIS"
E_INVALID_AGE = "INVALID_AGE"
E_INVALID_GENDER = "INVALID_GENDER"
E_NEWBORN_WEIGHT_MISSING = "NEWBORN_WEIGHT_MISSING"
E_NOT_GROUPED = "NOT_GROUPED"
E_AMBIGUOUS = "AMBIGUOUS"

_GENDER_MAP = {
    "m": "1",
    "male": "1",
    "1": "1",
    "男": "1",
    "男性": "1",
    "f": "2",
    "female": "2",
    "2": "2",
    "女": "2",
    "女性": "2",
}

_AGE_UNITS = {
    "years": "years",
    "year": "years",
    "y": "years",
    "岁": "years",
    "周岁": "years",
    "months": "months",
    "month": "months",
    "m": "months",
    "月": "months",
    "月龄": "months",
    "days": "days",
    "day": "days",
    "d": "days",
    "天": "days",
    "日": "days",
    "天龄": "days",
}

# 新生儿判定：天龄 <= 28 天（方案中所有新生儿相关的界值都是 XSRTL<29）
NEWBORN_MAX_DAYS = 28


class RecordError(ValueError):
    """单条病历不满足分组前置条件。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Case:
    patient_id: Optional[str]
    fields: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _code_of(item: Any) -> str:
    """兼容 {"code": "..."} 或直接给编码字符串两种写法。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return normalize_code(item)
    if isinstance(item, dict):
        return normalize_code(item.get("code") or item.get("编码") or "")
    return ""


def _list_of_codes(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        code = _code_of(item)
        if code and code not in out:
            out.append(code)
    return out


def _to_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _age_fields(record: Dict[str, Any], warnings: List[str]) -> Tuple[Optional[int], Optional[int]]:
    """把 age/age_unit 换算成方案里的 NL（周岁）与 XSRTL（天龄）。

    返回 (NL, XSRTL)；两者都是 int 或 None。
    """
    raw_age = record.get("age", None)
    age = _to_number(raw_age)
    if age is None:
        raise RecordError(E_INVALID_AGE, "缺少年龄 age，或 age 不是数字")

    unit_raw = _text(record.get("age_unit")).lower() or "years"
    unit = _AGE_UNITS.get(unit_raw)
    if unit is None:
        raise RecordError(
            E_INVALID_AGE,
            f"age_unit 只支持 years / months / days，收到 {record.get('age_unit')!r}",
        )
    if age < 0:
        raise RecordError(E_INVALID_AGE, "age 不能为负数")

    if unit == "days":
        days = int(round(age))
        if days > 47000:
            raise RecordError(E_INVALID_AGE, "age_unit=days 时 age 超出合理范围")
        return 0, days

    if unit == "months":
        months = age
        if months > 1560:
            raise RecordError(E_INVALID_AGE, "age_unit=months 时 age 超出合理范围")
        days = int(round(months * 30.4375))
        return (int(months // 12) if months >= 12 else 0), days

    # years
    years = int(age)
    if years > 130:
        raise RecordError(E_INVALID_AGE, "age_unit=years 时 age 超出合理范围")
    if years == 0:
        raise RecordError(
            E_INVALID_AGE,
            "age=0 岁无法区分新生儿与婴儿，请用 age_unit=months 或 age_unit=days 提供精确年龄",
        )
    return years, years * 365


def prepare_case(record: Dict[str, Any]) -> Case:
    """校验并规范化一条病历输入。"""
    if not isinstance(record, dict):
        raise RecordError(E_INVALID_INPUT, "单条病历必须是 JSON 对象")

    warnings: List[str] = []

    primary_dx = _code_of(record.get("primary_diagnosis"))
    if not primary_dx:
        raise RecordError(E_MISSING_PRIMARY_DIAGNOSIS, "缺少主要诊断 primary_diagnosis.code")

    gender_raw = _text(record.get("gender")).lower()
    gender = _GENDER_MAP.get(gender_raw, "")
    if not gender:
        raise RecordError(
            E_INVALID_GENDER,
            f"gender 只支持 M/F（或 1/2、男/女），收到 {record.get('gender')!r}",
        )

    nl, xsrtl = _age_fields(record, warnings)

    weight_raw = record.get("birth_weight_g", record.get("birth_weight", None))
    weight = _to_number(weight_raw)
    if weight is not None and weight <= 0:
        raise RecordError(E_INVALID_INPUT, "birth_weight_g 必须大于 0（单位：克）")

    primary_ops = _list_of_codes(record.get("primary_operation"))
    other_ops = _list_of_codes(record.get("other_operations"))
    other_ops = [c for c in other_ops if c not in primary_ops]
    other_dx = [c for c in _list_of_codes(record.get("other_diagnoses")) if c != primary_dx]

    if len(record.get("other_diagnoses") or []) != len(other_dx):
        warnings.append("其他诊断中存在重复或与主要诊断相同的编码，已自动去重")

    fields = {
        "ZYZD": primary_dx,
        "QTZD": other_dx,
        "ZYSS": primary_ops,
        "QTSS": other_ops,
        "XB": gender,
        "NL": nl,
        "XSRTL": xsrtl,
        "XSRTZ": int(round(weight)) if weight is not None else None,
    }

    meta = {
        "primary_diagnosis_name": _text((record.get("primary_diagnosis") or {}).get("name"))
        if isinstance(record.get("primary_diagnosis"), dict)
        else "",
        "newborn": xsrtl is not None and xsrtl <= NEWBORN_MAX_DAYS,
    }

    return Case(
        patient_id=_text(record.get("patient_id")) or None,
        fields=fields,
        warnings=warnings,
        meta=meta,
    )


class DrgGrouper:
    """分组器主体。线程安全：只读配置，可被 HTTP 服务的多个线程共享。"""

    def __init__(self, config: GrouperConfig):
        self.config = config
        self.stats = {"total": 0, "grouped": 0, "not_grouped": 0, "last_at": None}
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def group_record(self, record: Dict[str, Any], debug: bool = False) -> Dict[str, Any]:
        """对一条病历分组，任何记录级问题都以 is_grouped=false 返回，不抛异常。"""
        started = time.perf_counter()
        try:
            case = prepare_case(record)
        except RecordError as exc:
            patient_id = None
            if isinstance(record, dict):
                patient_id = _text(record.get("patient_id")) or None
            return self._not_grouped(
                patient_id=patient_id,
                reason_code=exc.code,
                message=exc.message,
                warnings=[],
                started=started,
            )

        fields = case.fields
        warnings = list(case.warnings)

        # 新生儿必须提供出生体重，否则无法在 PS1–PS5 之间分档
        if case.meta["newborn"] and fields["XSRTZ"] is None:
            return self._not_grouped(
                patient_id=case.patient_id,
                reason_code=E_NEWBORN_WEIGHT_MISSING,
                message=(
                    f"新生儿（天龄 {fields['XSRTL']} 天）未提供出生体重 birth_weight_g（单位：克），"
                    "无法在早产儿/新生儿体重分档（PS1–PS5）中确定组别"
                ),
                warnings=warnings,
                started=started,
            )

        mcc, cc, cc_details = self._cc_status(fields)

        context = {
            "fields": fields,
            "sets": self.config.sets,
            "mcc": mcc,
            "cc": cc,
        }

        trace: List[Dict[str, Any]] = []
        outcome = self._resolve(context, trace)
        self._bump(outcome["is_grouped"])

        result: Dict[str, Any] = {
            "is_grouped": outcome["is_grouped"],
            "grouping_result": {
                "patient_id": case.patient_id,
                "mdc": outcome["mdc"],
                "adrg": outcome["adrg"],
                "drg": outcome["drg"],
                "rw": RW_VALUE,
            },
        }
        if not outcome["is_grouped"]:
            result["reason"] = {"code": outcome["reason_code"], "message": outcome["message"]}
        if warnings:
            result["warnings"] = warnings

        if debug:
            result["diagnostics"] = {
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "cc_status": "MCC" if mcc else ("CC" if cc else "无MCC/CC"),
                "valid_mcc": sorted(mcc),
                "valid_cc": sorted(cc),
                "cc_details": cc_details,
                "trace": trace,
                "fields": {
                    "primary_diagnosis": fields["ZYZD"],
                    "other_diagnoses": fields["QTZD"],
                    "primary_operation": fields["ZYSS"],
                    "other_operations": fields["QTSS"],
                    "gender": fields["XB"],
                    "age_years": fields["NL"],
                    "neonatal_days": fields["XSRTL"],
                    "birth_weight_g": fields["XSRTZ"],
                },
            }
        return result

    def group_batch(self, records: Sequence[Dict[str, Any]], debug: bool = False) -> Dict[str, Any]:
        started = time.perf_counter()
        results = [self.group_record(record, debug=debug) for record in records]
        grouped = sum(1 for r in results if r["is_grouped"])
        return {
            "summary": {
                "total": len(results),
                "grouped": grouped,
                "not_grouped": len(results) - grouped,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            "results": results,
        }

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _cc_status(self, fields: Dict[str, Any]) -> Tuple[set, set, List[Dict[str, Any]]]:
        """判定有效 MCC / CC：其他诊断命中 CC 表，且主要诊断不在其排除表中。"""
        primary = fields["ZYZD"]
        mcc: set = set()
        cc: set = set()
        details: List[Dict[str, Any]] = []
        for code in fields["QTZD"]:
            row = self.config.cc_by_code.get(code)
            if not row:
                continue
            ex_name = row.get("ex") or ""
            ex_set = self.config.exclusions.get(ex_name) if ex_name else None
            excluded = bool(ex_set) and primary in ex_set
            kind = row.get("type")
            if kind not in ("MCC", "CC"):
                continue
            if not excluded:
                (mcc if kind == "MCC" else cc).add(code)
            details.append(
                {
                    "code": code,
                    "name": row.get("name", ""),
                    "type": kind,
                    "excluded": excluded,
                    "exclusion_table": ex_name or None,
                }
            )
        return mcc, cc, details

    def _resolve(self, context: Dict[str, Any], trace: List[Dict[str, Any]]) -> Dict[str, Any]:
        cfg = self.config

        # 1) 先期分组
        for row in cfg.adrg_by_mdc.get("MDCA", []):
            if row["ast"] is None:
                continue
            if evaluate(row["ast"], context):
                trace.append({"stage": "MDCA", "code": row["code"], "name": cfg.display_name("ADRG", row["code"], row["name"])})
                return self._finish(row["mdc"], row, context, trace)

        # 2) MDC
        for mdc in sorted(cfg.mdc_rows, key=lambda r: (r["order"], r["seq"])):
            if mdc["code"] in ("MDCA", "0000") or mdc["ast"] is None:
                continue
            if evaluate(mdc["ast"], context):
                trace.append({"stage": "MDC", "code": mdc["code"], "name": mdc["name"]})
                return self._within_mdc(mdc, context, trace)

        return {
            "is_grouped": False,
            "mdc": None,
            "adrg": None,
            "drg": None,
            "reason_code": E_NOT_GROUPED,
            "message": "主要诊断未匹配任何 MDC（请检查诊断编码是否在 3.0 版主诊表内）",
        }

    def _within_mdc(
        self, mdc: Dict[str, Any], context: Dict[str, Any], trace: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        cfg = self.config
        for row in cfg.adrg_by_mdc.get(mdc["code"], []):
            if row["ast"] is None:
                # x00 兜底：该 MDC 下没有匹配到任何 ADRG
                trace.append({"stage": "ADRG_FALLBACK", "code": row["code"]})
                return {
                    "is_grouped": False,
                    "mdc": self._node(mdc),
                    "adrg": None,
                    "drg": None,
                    "reason_code": E_NOT_GROUPED,
                    "message": f"已归入 {mdc['code']}（{mdc['name']}），但未匹配到任何 ADRG",
                }
            if not evaluate(row["ast"], context):
                continue

            trace.append({"stage": "ADRG", "code": row["code"], "name": cfg.display_name("ADRG", row["code"], row["name"])})

            if row["code"].endswith("QY"):
                return {
                    "is_grouped": False,
                    "mdc": self._node(mdc),
                    "adrg": self._node(row),
                    "drg": None,
                    "reason_code": E_AMBIGUOUS,
                    "message": (
                        "主要手术属于分组内涵列表，但主诊断所在 MDC 内没有对应的外科/操作组，"
                        "按方案判为歧义组（xQY）"
                    ),
                }

            drg_row = self._pick_drg(row, context, trace)
            if drg_row is None:
                return {
                    "is_grouped": False,
                    "mdc": self._node(mdc),
                    "adrg": self._node(row),
                    "drg": None,
                    "reason_code": E_NOT_GROUPED,
                    "message": f"已匹配 ADRG {row['code']}，但该 ADRG 下没有可用的 DRG 行",
                }
            return {
                "is_grouped": True,
                "mdc": self._node(mdc),
                "adrg": self._node(row),
                "drg": self._node(drg_row),
                "reason_code": None,
                "message": None,
            }

        return {
            "is_grouped": False,
            "mdc": self._node(mdc),
            "adrg": None,
            "drg": None,
            "reason_code": E_NOT_GROUPED,
            "message": f"已归入 {mdc['code']}，但该 MDC 下没有可用的 ADRG 行",
        }

    def _finish(
        self, mdc_code: str, adrg_row: Dict[str, Any], context: Dict[str, Any], trace: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """先期分组命中后，选 DRG。"""
        drg_row = self._pick_drg(adrg_row, context, trace)
        mdc = self.config.mdc_by_code.get(mdc_code)
        if drg_row is None:
            return {
                "is_grouped": False,
                "mdc": self._node(mdc) if mdc else None,
                "adrg": self._node(adrg_row),
                "drg": None,
                "reason_code": E_NOT_GROUPED,
                "message": f"已匹配 ADRG {adrg_row['code']}，但该 ADRG 下没有可用的 DRG 行",
            }
        return {
            "is_grouped": True,
            "mdc": self._node(mdc) if mdc else None,
            "adrg": self._node(adrg_row),
            "drg": self._node(drg_row),
            "reason_code": None,
            "message": None,
        }

    def _pick_drg(
        self, adrg_row: Dict[str, Any], context: Dict[str, Any], trace: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        for drg in self.config.drg_by_adrg.get(adrg_row["code"], []):
            if drg["ast"] is None:
                trace.append({"stage": "DRG_DEFAULT", "code": drg["code"]})
                return drg
            if evaluate(drg["ast"], context):
                trace.append({"stage": "DRG", "code": drg["code"], "rule": drg["rule"]})
                return drg
        return None

    def _node(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not row:
            return None
        return {
            "code": row["code"],
            "name": self.config.display_name(row["kind"], row["code"], row["name"]),
        }

    def _not_grouped(
        self,
        patient_id: Optional[str],
        reason_code: str,
        message: str,
        warnings: List[str],
        started: float,
    ) -> Dict[str, Any]:
        self._bump(False)
        result: Dict[str, Any] = {
            "is_grouped": False,
            "grouping_result": {
                "patient_id": patient_id,
                "mdc": None,
                "adrg": None,
                "drg": None,
                "rw": RW_VALUE,
            },
            "reason": {"code": reason_code, "message": message},
        }
        if warnings:
            result["warnings"] = warnings
        return result

    def _bump(self, grouped: bool) -> None:
        with self._stats_lock:
            self.stats["total"] += 1
            if grouped:
                self.stats["grouped"] += 1
            else:
                self.stats["not_grouped"] += 1
            self.stats["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
