# -*- coding: utf-8 -*-
"""端到端自检：进程内起服务（不占用常驻端口），验证版本、单条、批量、CSV、页面。"""

import json
import sys
import threading
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import APP_VERSION, create_server
from grouper import DrgGrouper, load_config


def call(method, url, body=None, ctype="application/json; charset=utf-8"):
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read()


def main() -> int:
    server = create_server("127.0.0.1", 0, DrgGrouper(load_config()), quiet=True)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{port}"
    failures = []
    try:
        _, body = call("GET", f"{root}/api/v1/health")
        health = json.loads(body)
        print(f"health  : {health['status']} / version {health['version']}")
        if health["version"] != APP_VERSION:
            failures.append("health 版本不一致")

        _, body = call("GET", f"{root}/api/v1/version")
        info = json.loads(body)
        print(f"version : app={info['app_version']} adrg={info['config']['adrg']} drg={info['config']['drg']}")
        if info["app_version"] != APP_VERSION:
            failures.append("version 不一致")

        record = {
            "patient_id": "E2E-1", "age": 65, "age_unit": "years", "gender": "M",
            "primary_diagnosis": {"code": "I63.900"},
            "primary_operation": {"code": "38.1201"},
        }
        _, body = call("POST", f"{root}/api/v1/group", json.dumps(record))
        result = json.loads(body)
        gr = result["grouping_result"]
        print(f"group   : {gr['mdc']['code']}/{gr['adrg']['code']}/{gr['drg']['code']} rw={gr['rw']}")
        if (gr["mdc"]["code"], gr["adrg"]["code"], gr["drg"]["code"]) != ("MDCB", "BE1", "BE19"):
            failures.append("单条分组结果异常")

        _, body = call("POST", f"{root}/api/v1/group/batch", json.dumps([record, {"age": 1}]))
        batch = json.loads(body)
        print(f"batch   : total={batch['summary']['total']} grouped={batch['summary']['grouped']}")
        if batch["summary"]["total"] != 2:
            failures.append("批量汇总异常")

        csv_text = (
            "patient_id,age,age_unit,gender,birth_weight_g,primary_diagnosis_code,"
            "primary_operation_code\nN1,3,days,F,1800,P07.200,\n"
        )
        _, body = call("POST", f"{root}/api/v1/group/csv", csv_text, "text/csv; charset=utf-8")
        csv_result = json.loads(body)
        adrg = csv_result["results"][0]["grouping_result"]["adrg"]["code"]
        print(f"csv     : {adrg}")
        if adrg != "PS3":
            failures.append("CSV 新生儿分档异常")

        _, body = call("GET", f"{root}/")
        if "CHS-DRG 3.0 分组测试".encode("utf-8") not in body:
            failures.append("首页内容异常")
        print(f"page    : {len(body)} bytes")
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print("失败：" + "；".join(failures))
        return 1
    print("端到端自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
