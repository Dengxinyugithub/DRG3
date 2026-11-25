# -*- coding: utf-8 -*-
"""接口层测试：在进程内起服务，逐个打接口（python tests/test_api.py）。"""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import create_server  # noqa: E402
from grouper import DrgGrouper, load_config  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name} {detail}")


def request(method, url, body=None, content_type="application/json; charset=utf-8"):
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


config = load_config()
grouper = DrgGrouper(config)
server = create_server("127.0.0.1", 0, grouper, quiet=True)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
root = f"http://127.0.0.1:{port}"
print(f"测试服务：{root}")

try:
    # 1. 健康检查
    status, headers, body = request("GET", f"{root}/api/v1/health")
    check("health 状态码", status == 200)
    check("health CORS", headers.get("Access-Control-Allow-Origin") == "*")
    check("health 内容", json.loads(body)["status"] == "healthy")

    # 2. 版本/配置
    status, _, body = request("GET", f"{root}/api/v1/version")
    check("version 状态码", status == 200)
    check("version 配置规模", json.loads(body)["config"]["adrg"] == 538)

    # 3. 首页
    status, headers, body = request("GET", f"{root}/")
    check("首页状态码", status == 200)
    check("首页是 HTML", "text/html" in headers.get("Content-Type", ""))
    check("首页包含标题", "CHS-DRG 3.0 分组测试".encode("utf-8") in body)

    # 4. 单条分组
    sample = {
        "patient_id": "345899002", "age": 65, "age_unit": "years", "gender": "M",
        "primary_diagnosis": {"code": "I63.900"},
        "other_diagnoses": [{"code": "J96.000"}],
        "primary_operation": {"code": "38.1201"},
    }
    status, _, body = request("POST", f"{root}/api/v1/group", json.dumps(sample))
    data = json.loads(body)
    check("单条 状态码", status == 200)
    check("单条 结构", set(data) >= {"is_grouped", "grouping_result"})
    check("单条 rw", data["grouping_result"]["rw"] == "NAN")
    check("单条 入组", data["is_grouped"] is True and data["grouping_result"]["drg"]["code"] == "BE19")

    # 5. debug 参数
    status, _, body = request("POST", f"{root}/api/v1/group?debug=1", json.dumps(sample))
    data = json.loads(body)
    check("debug 返回诊断信息", "diagnostics" in data and data["diagnostics"]["cc_status"] == "MCC")

    # 6. 批量
    status, _, body = request("POST", f"{root}/api/v1/group/batch", json.dumps([sample, {"age": 1}]))
    data = json.loads(body)
    check("批量 状态码", status == 200)
    check("批量 汇总", data["summary"]["total"] == 2 and data["summary"]["grouped"] == 1)
    check("批量 无中断", data["results"][1]["is_grouped"] is False)

    # 7. 批量 records 包装
    status, _, body = request("POST", f"{root}/api/v1/group/batch", json.dumps({"records": [sample]}))
    check("批量 records 包装", json.loads(body)["summary"]["total"] == 1)

    # 8. CSV（JSON 输出）
    csv_text = (
        "patient_id,age,age_unit,gender,birth_weight_g,primary_diagnosis_code,"
        "primary_diagnosis_name,other_diagnosis_codes,other_diagnosis_names,"
        "primary_operation_code,primary_operation_name,other_operation_codes,other_operation_names\n"
        "S1,65,years,M,,I63.900,脑梗死,,,,38.1201,颈动脉内膜剥脱术,,\n"
        "S2,3,days,F,1800,P07.200,早产,,,,,,,\n"
    )
    status, _, body = request("POST", f"{root}/api/v1/group/csv", csv_text, "text/csv; charset=utf-8")
    data = json.loads(body)
    check("CSV 状态码", status == 200)
    check("CSV 解析条数", data["summary"]["total"] == 2)
    check("CSV 新生儿体重分档", data["results"][1]["grouping_result"]["adrg"]["code"] == "PS3")

    # 8.1 Excel 另存的 UTF-8 BOM 文件
    status, _, body = request("POST", f"{root}/api/v1/group/csv", "\ufeff" + csv_text, "text/csv; charset=utf-8")
    check("CSV 带 BOM 可解析", json.loads(body)["summary"]["grouped"] == 2)

    # 8.2 GB18030 编码的 CSV
    status, _, body = request(
        "POST", f"{root}/api/v1/group/csv", csv_text.encode("gb18030"), "text/csv; charset=utf-8"
    )
    check("CSV GB18030 可解析", json.loads(body)["summary"]["grouped"] == 2)

    # 9. CSV 输出
    status, headers, body = request(
        "POST", f"{root}/api/v1/group/csv?output=csv", csv_text, "text/csv; charset=utf-8"
    )
    check("CSV 输出类型", "text/csv" in headers.get("Content-Type", ""))
    check("CSV 输出表头", body.decode("utf-8-sig").splitlines()[0].startswith("patient_id,is_grouped"))

    # 10. 编码检索
    status, _, body = request("GET", f"{root}/api/v1/codes/search?type=dx&q=%E8%84%91%E6%A2%97&limit=5")
    data = json.loads(body)
    check("检索 状态码", status == 200)
    check("检索 有结果", data["count"] > 0 and "code" in data["items"][0])

    # 11. 模板
    status, headers, body = request("GET", f"{root}/api/v1/template/csv")
    check("模板 状态码", status == 200)
    check("模板 有表头", "patient_id" in body.decode("utf-8-sig").splitlines()[0])

    # 12. 错误处理
    status, _, body = request("POST", f"{root}/api/v1/group", "{bad json")
    check("坏 JSON 400", status == 400 and json.loads(body)["error"]["code"] == "BAD_JSON")

    status, _, body = request("POST", f"{root}/api/v1/group", json.dumps([1, 2]))
    check("单条接口拒绝数组", status == 400)

    status, _, body = request("GET", f"{root}/api/v1/nope")
    check("404", status == 404)

    # 13. OPTIONS 预检
    status, headers, _ = request("OPTIONS", f"{root}/api/v1/group")
    check("OPTIONS 204", status == 204)
    check("OPTIONS 允许方法", "POST" in headers.get("Access-Control-Allow-Methods", ""))

    # 14. 统计
    status, _, body = request("GET", f"{root}/api/v1/health")
    stats = json.loads(body)["stats"]
    check("统计已累计", stats["total"] >= 6, stats)
finally:
    server.shutdown()
    server.server_close()

print(f"\n通过 {PASSED} 项，失败 {len(FAILED)} 项")
for item in FAILED:
    print("  [FAIL]", item)
if FAILED:
    raise SystemExit(1)
print("全部通过")
