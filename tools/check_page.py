# -*- coding: utf-8 -*-
"""静态检查测试页：JS 里引用的元素 id 是否都存在、调用的接口是否都已实现。

用法（任意平台）：
    python tools/check_page.py
"""

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
html = (BASE / "web" / "index.html").read_text(encoding="utf-8")
js = re.findall(r"<script>(.*?)</script>", html, re.S)[0]
app = (BASE / "app.py").read_text(encoding="utf-8")

ids_in_html = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
ids_used = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)', js))
ids_used |= set(re.findall(r'getElementById\("([A-Za-z0-9_\-]+)"\)', js))
missing = sorted(i for i in ids_used if i not in ids_in_html)
print("元素 id 引用:", len(ids_used), "缺失:", missing or "无")

paths = sorted(set(re.findall(r'api\("(/api/[^"?]+)', js)))
print("接口调用:", paths)
missing_api = []
for path in paths:
    needle = path.rstrip("/")
    if needle not in app and needle.replace("/api/v1/", "") not in app:
        missing_api.append(path)
print("接口缺失:", missing_api or "无")

bindings = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)\.addEventListener', js))
print("绑定事件的元素缺失:", sorted(b for b in bindings if b not in ids_in_html) or "无")

missing_fn = [
    fn
    for fn in (
        "function group(",
        "function runBatch(",
        "function uploadCsv(",
        "function renderResult(",
        "function buildRecord(",
        "function attachSearch(",
        "function ping(",
    )
    if fn not in js
]
print("关键函数缺失:", missing_fn or "无")

# 页面与服务端副本必须一致
root_copy = BASE / "DRG分组测试.html"
if root_copy.exists():
    same = root_copy.read_bytes() == (BASE / "web" / "index.html").read_bytes()
    print("根目录 HTML 与服务内置页一致:", "是" if same else "否（需重新复制）")
    if not same:
        missing.append("DRG分组测试.html 不同步")

failed = bool(missing or missing_api or missing_fn)
print("检查完成：" + ("存在问题" if failed else "全部通过"))
sys.exit(1 if failed else 0)
