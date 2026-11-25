# -*- coding: utf-8 -*-
"""分组引擎测试（纯标准库，直接运行：python tests/test_engine.py）。"""

import random
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from grouper import DrgGrouper, load_config  # noqa: E402

PASSED = 0
FAILED = []
CONFIG = load_config()
GROUPER = DrgGrouper(CONFIG)


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name} {detail}")


def group(record, debug=False):
    return GROUPER.group_record(record, debug=debug)


def expect(name, record, **want):
    result = group(record)
    gr = result["grouping_result"]
    ok = True
    for key, value in want.items():
        if key == "reason":
            actual = (result.get("reason") or {}).get("code")
        elif key == "rw":
            actual = gr.get("rw")
        elif key in ("mdc", "adrg", "drg"):
            node = gr.get(key) or {}
            actual = node.get("code")
        elif key == "is_grouped":
            actual = result.get("is_grouped")
        else:
            raise KeyError(key)
        if actual != value:
            ok = False
            check(name, False, f"期望 {key}={value!r}，实际 {actual!r}")
            break
    if ok:
        check(name, True)


# --------------------------------------------------------------------------- #
# 1. 配置规模
# --------------------------------------------------------------------------- #
summary = CONFIG.summary()
check("配置-MDC 数量", summary["mdc"] == 27, summary["mdc"])
check("配置-ADRG 数量", summary["adrg"] == 538, summary["adrg"])
check("配置-DRG 数量", summary["drg"] == 871, summary["drg"])
check("配置-CC/MCC 数量", summary["cc_mcc"] == 7918, summary["cc_mcc"])
check("配置-编码集合数量", summary["sets"] == 634, summary["sets"])
check("配置-集合编码总数", summary["set_codes"] == 106837, summary["set_codes"])

# --------------------------------------------------------------------------- #
# 2. 典型病例
# --------------------------------------------------------------------------- #
SAMPLE = {
    "patient_id": "345899002",
    "age": 65,
    "age_unit": "years",
    "gender": "M",
    "primary_diagnosis": {"code": "I63.900", "name": "脑梗死"},
    "other_diagnoses": [
        {"code": "E11.900", "name": "2型糖尿病"},
        {"code": "I10.000", "name": "高血压病"},
        {"code": "J96.000", "name": "急性呼吸衰竭"},
    ],
    "primary_operation": {"code": "38.1201", "name": "颈动脉内膜剥脱术"},
    "other_operations": [
        {"code": "88.4100", "name": "脑血管造影"},
        {"code": "96.7201", "name": "有创呼吸机支持治疗"},
    ],
}
expect("示例病例", SAMPLE, is_grouped=True, mdc="MDCB", adrg="BE1", drg="BE19", rw="NAN")

expect(
    "阑尾炎+阑尾切除",
    {
        "patient_id": "A001", "age": 30, "age_unit": "years", "gender": "M",
        "primary_diagnosis": {"code": "K35.800x001"},
        "primary_operation": {"code": "47.0901"},
    },
    is_grouped=True, mdc="MDCG", adrg="GE2", drg="GE29",
)

# 29 天≤年龄＜1 岁、新生儿期诊断、无主要手术 —— 原单机版在此处会"未入组"，本实现修正
expect(
    "婴儿 90 天无手术(PV1)",
    {"patient_id": "B001", "age": 90, "age_unit": "days", "gender": "M",
     "primary_diagnosis": {"code": "A04.000x001"}},
    is_grouped=True, mdc="MDCP", adrg="PV1", drg="PV15",
)
expect(
    "婴儿 3 月龄无手术(PV1)",
    {"patient_id": "B002", "age": 3, "age_unit": "months", "gender": "F",
     "primary_diagnosis": {"code": "A04.000x001"}},
    is_grouped=True, mdc="MDCP", adrg="PV1",
)

# 新生儿：无体重直接无法分组
expect(
    "新生儿无体重",
    {"patient_id": "C001", "age": 10, "age_unit": "days", "gender": "M",
     "primary_diagnosis": {"code": "P07.200"}},
    is_grouped=False, reason="NEWBORN_WEIGHT_MISSING", rw="NAN",
)
# 新生儿：有体重按档分组（1800g -> PS3 1500-1999g）
expect(
    "新生儿 1800g",
    {"patient_id": "C002", "age": 10, "age_unit": "days", "gender": "M", "birth_weight_g": 1800,
     "primary_diagnosis": {"code": "P07.200"}},
    is_grouped=True, mdc="MDCP", adrg="PS3",
)
# 体重对非新生儿无影响
expect(
    "非新生儿体重被忽略",
    {"patient_id": "C003", "age": 40, "age_unit": "years", "gender": "M", "birth_weight_g": 3000,
     "primary_diagnosis": {"code": "K35.800x001"}, "primary_operation": {"code": "47.0901"}},
    is_grouped=True, adrg="GE2",
)

# CC / MCC
expect(
    "伴一般合并症(CC)",
    {"patient_id": "P1", "age": 90, "age_unit": "days", "gender": "M",
     "primary_diagnosis": {"code": "A04.000x001"},
     "other_diagnoses": [{"code": "A15.000x001"}]},
    is_grouped=True, adrg="PV1", drg="PV13",
)
expect(
    "伴严重合并症(MCC)",
    {"patient_id": "P2", "age": 90, "age_unit": "days", "gender": "M",
     "primary_diagnosis": {"code": "A04.000x001"},
     "other_diagnoses": [{"code": "A19.000"}]},
    is_grouped=True, adrg="PV1", drg="PV11",
)
expect(
    "MCC 被主要诊断排除",
    {"patient_id": "P3", "age": 70, "age_unit": "years", "gender": "M",
     "primary_diagnosis": {"code": "J95.000"},
     "other_diagnoses": [{"code": "J96.000"}]},
    is_grouped=True, drg="DZ19",
)

# 机器人辅助手术：主要手术填术式、机器人编码放其他手术
expect(
    "机器人辅助肺手术",
    {"patient_id": "R1", "age": 60, "age_unit": "years", "gender": "M",
     "primary_diagnosis": {"code": "C34.900x001"},
     "primary_operation": {"code": "32.4100"},
     "other_operations": [{"code": "17.4100"}]},
    is_grouped=True, mdc="MDCE", adrg="EB1", drg="EB10",
)

# 高危妊娠：OB2 伴严重合并症
expect(
    "重度子痫前期+剖宫产",
    {"patient_id": "O1", "age": 28, "age_unit": "years", "gender": "F",
     "primary_diagnosis": {"code": "O14.100x002"},
     "primary_operation": {"code": "74.1x01"}},
    is_grouped=True, mdc="MDCO", adrg="OB2", drg="OB21",
)

# 歧义组：主诊断在 MDCG，主要手术是胸科手术
expect(
    "歧义组",
    {"patient_id": "Q1", "age": 40, "age_unit": "years", "gender": "M",
     "primary_diagnosis": {"code": "K35.800x001"},
     "primary_operation": {"code": "32.4100"}},
    is_grouped=False, mdc="MDCG", adrg="GQY", reason="AMBIGUOUS",
)
# 未入组：诊断不在主诊表
expect(
    "未入组",
    {"patient_id": "N1", "age": 40, "age_unit": "years", "gender": "M",
     "primary_diagnosis": {"code": "ZZZ.999"}},
    is_grouped=False, reason="NOT_GROUPED",
)

# 入参校验
expect(
    "缺主要诊断",
    {"patient_id": "E1", "age": 40, "age_unit": "years", "gender": "M"},
    is_grouped=False, reason="MISSING_PRIMARY_DIAGNOSIS",
)
expect(
    "缺性别",
    {"patient_id": "E2", "age": 40, "age_unit": "years",
     "primary_diagnosis": {"code": "K35.800x001"}},
    is_grouped=False, reason="INVALID_GENDER",
)
expect(
    "年龄为 0 岁",
    {"patient_id": "E3", "age": 0, "age_unit": "years", "gender": "M",
     "primary_diagnosis": {"code": "K35.800x001"}},
    is_grouped=False, reason="INVALID_AGE",
)
expect(
    "年龄单位非法",
    {"patient_id": "E4", "age": 40, "age_unit": "hours", "gender": "M",
     "primary_diagnosis": {"code": "K35.800x001"}},
    is_grouped=False, reason="INVALID_AGE",
)

# 性别写法兼容
for gender in ("M", "m", "1", "男", "男性"):
    expect(
        f"性别写法 {gender}",
        {"patient_id": "G1", "age": 30, "age_unit": "years", "gender": gender,
         "primary_diagnosis": {"code": "K35.800x001"}, "primary_operation": {"code": "47.0901"}},
        is_grouped=True, adrg="GE2",
    )

# 编码规范化：X 与 x 等价
expect(
    "编码大小写 X 兼容",
    {"patient_id": "H1", "age": 30, "age_unit": "years", "gender": "M",
     "primary_diagnosis": {"code": "K35.800X001"}, "primary_operation": {"code": "47.0901"}},
    is_grouped=True, adrg="GE2",
)

# --------------------------------------------------------------------------- #
# 3. 随机稳健性：随机组合主诊断/手术，不允许抛异常，结构必须完整
# --------------------------------------------------------------------------- #
random.seed(20261123)
diag_pool = sorted(CONFIG.sets["DI_B00"])[:40] + sorted(CONFIG.sets["DI_G00"])[:40] + sorted(CONFIG.sets["DI_E00"])[:40]
op_pool = sorted(CONFIG.sets["OP_ALL"])[:60]
errors = 0
for i in range(300):
    record = {
        "patient_id": f"RND{i}",
        "age": random.choice([0, 1, 3, 10, 30, 65, 80]),
        "age_unit": random.choice(["years", "months", "days"]),
        "gender": random.choice(["M", "F"]),
        "primary_diagnosis": {"code": random.choice(diag_pool)},
        "other_diagnoses": [{"code": c} for c in random.sample(sorted(CONFIG.sets["DI_K00"])[:30], 2)],
    }
    if random.random() < 0.7:
        record["primary_operation"] = {"code": random.choice(op_pool)}
    if random.random() < 0.3:
        record["birth_weight_g"] = random.choice([800, 1200, 1800, 2400, 3000])
    try:
        out = GROUPER.group_record(record, debug=True)
        gr = out["grouping_result"]
        assert isinstance(out["is_grouped"], bool)
        assert gr["rw"] == "NAN"
        assert set(gr) == {"patient_id", "mdc", "adrg", "drg", "rw"}
        if out["is_grouped"]:
            assert gr["mdc"] and gr["adrg"] and gr["drg"], "入组病例缺少节点"
        else:
            assert out["reason"]["code"], "未入组缺少原因"
    except Exception as exc:  # noqa: BLE001
        errors += 1
        if errors <= 3:
            print("随机用例异常：", record, exc)
check("随机 300 例稳健性", errors == 0, f"异常 {errors} 例")

# --------------------------------------------------------------------------- #
# 4. 批量接口汇总
# --------------------------------------------------------------------------- #
batch = GROUPER.group_batch([SAMPLE, SAMPLE, {"patient_id": "bad"}])
check("批量汇总 total", batch["summary"]["total"] == 3)
check("批量汇总 grouped", batch["summary"]["grouped"] == 2)
check("批量逐条不中断", batch["results"][2]["is_grouped"] is False)

# 非法条目不能拖垮整批
weird = GROUPER.group_batch([1, None, SAMPLE])
check("批量含非法条目", weird["summary"]["total"] == 3 and weird["summary"]["grouped"] == 1)
check("非字典输入", GROUPER.group_record(123)["reason"]["code"] == "INVALID_INPUT")

# 字符串数字、大小写写法
expect(
    "字符串形式的年龄与体重",
    {"patient_id": "S1", "age": "10", "age_unit": "DAYS", "gender": "f",
     "birth_weight_g": "1800", "primary_diagnosis": {"code": "p07.200"}},
    is_grouped=True, mdc="MDCP", adrg="PS3",
)

# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
for item in FAILED:
    print("  [FAIL]", item)
if FAILED:
    raise SystemExit(1)
print("全部通过")
