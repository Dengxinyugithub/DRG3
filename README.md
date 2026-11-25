# CHS-DRG 3.0 本地分组器

基于国家医保局《按病组（DRG）付费3.0版分组方案配置信息》实现的**纯本地**分组器：不连数据库、不访问外网、不落盘病历数据，提供 HTTP 接口做单条/批量分组，并附带一个浏览器测试页。

- 配置规模：MDC 27、ADRG 538（含兜底组）、DRG 871、编码集合 634 个（106,837 条编码）、CC/MCC 7,918 条、排除表 148 张（21,036 条）
- 分组速度：实测约 0.16 ms/条，单进程约 5000 条/秒（5 条一批 0.3 ms 级）
- 数据流向：病历 → 本机 Python 进程 → 结果，全程不出机器

---

## 1. 快速开始

支持 **Windows / macOS / Linux**，三种方式任选，效果相同（启动本地服务 + 打开测试页）。

### 方式一：双击 / 命令行启动（推荐）

| 系统 | 操作 |
|---|---|
| Windows | 双击 `start.bat` |
| macOS | 双击 `start.command`；或终端执行 `./start.sh` |
| Linux | 执行 `./start.sh` |

> 首次使用 macOS / Linux 需要给脚本加执行权限（只需一次）：
> `chmod +x start.sh start.command`

### 方式二：直接用 Python 启动（全平台一致）

```bash
cd /path/to/DRG3.0分组器
python start.py                 # 默认 127.0.0.1:8765，并打开浏览器
python start.py --no-open       # 服务器/无桌面环境：不打开浏览器
python start.py --port 9000     # 换端口
python start.py --rebuild       # 配置 xlsx 更新后重建缓存
python start.py --check         # 只校验配置、打印规模，不启动服务
```

macOS / Linux 上 `python` 通常叫 `python3`，两个名字都可以：

```bash
python3 start.py
```

也可以绕过启动器直接跑服务：

```bash
python app.py --port 8765 --open      # Windows
python3 app.py --port 8765 --open     # macOS / Linux
```

浏览器访问 <http://127.0.0.1:8765/> 即为测试页面；也可以直接打开 `DRG分组测试.html`（它会连 127.0.0.1:8765）。

> 环境要求：**Python 3.10+**（Windows / macOS / Linux 均可；实测 Windows 11 + Python 3.12，macOS/Linux 使用同一套纯标准库代码，无平台限定 API）。
> 仅首次解析 xlsx 需要 `openpyxl`；缓存生成后不再依赖它。
>
> ```bash
> python -m pip install openpyxl        # Windows
> python3 -m pip install openpyxl       # macOS / Linux
> ```

### 跨平台是怎么做的

三个启动脚本都只是“薄壳”，只负责定位目录、挑一个 Python 解释器，然后把参数交给 `start.py`：

| 文件 | 平台 | 实际做的事 |
|---|---|---|
| `start.bat` | Windows | `chcp 65001` + `python start.py %*` |
| `start.sh` | macOS / Linux | `cd "$(dirname "$0")"` + `exec python3 start.py "$@"` |
| `start.command` | macOS 双击 | 调用 `start.sh`，保留终端窗口以便看报错 |

平台差异全部集中在 `start.py` 和 `app.py` 的两处，核心分组逻辑（`grouper/`）不区分平台：

- 控制台编码：Windows 默认 GBK，启动时统一 `reconfigure(encoding="utf-8")`，中文横幅不会抛异常；
- 解释器命令名：`start.sh` 优先 `python3`，找不到再退回 `python`；
- 依赖提示：Windows 给 `py -m pip`，其它系统给 `python3 -m pip`；
- 退出方式：前台 `Ctrl+C`，服务方式 `SIGTERM`（systemd / docker / launchctl 都能优雅退出）；
- 路径：全程 `pathlib.Path`，中文目录名可用。

两个只在 POSIX 上会遇到的坑，已在《维护文档》§9.4 写了处理办法：**脚本执行权限**（`chmod +x`）和 **CRLF 换行**（`sed -i 's/\r$//'`）。

---

## 2. 目录结构

```
DRG3.0分组器/
├─ app.py                     HTTP 服务入口（纯标准库，跨平台）
├─ start.py                   跨平台启动器（平台检测 / 依赖检查 / 参数转发）
├─ start.bat                  Windows 双击启动
├─ start.sh                   macOS / Linux 启动脚本
├─ start.command              macOS 双击启动
├─ DRG分组测试.html            浏览器测试页（与 web/index.html 相同，可单独打开）
├─ grouper/
│  ├─ rules.py                规则表达式解析与求值
│  ├─ config.py               xlsx 读取、校验、缓存
│  └─ engine.py               分组主流程（MDCA → MDC → ADRG → DRG）
├─ data/
│  ├─ DRG3.0分组方案配置信息.xlsx   国家医保局原始配置
│  └─ config.cache.json.gz    首次启动自动生成的缓存（可删，会重建）
├─ web/index.html             服务内置测试页
├─ tests/
│  ├─ test_engine.py          引擎测试（37 项）
│  └─ test_api.py             接口测试（34 项）
├─ tools/                     开发期辅助脚本（selftest 端到端 / test_csv_page 页面 CSV 逻辑 / bench 吞吐 / check_page 页面检查）
├─ README.md
├─ 接口文档.md
└─ 维护文档.md                 换版 SOP、排障、发布与回滚
```

---

## 3. 输入格式

一条病历一个 JSON 对象：

```json
{
  "patient_id": "345899002",
  "age": 65,
  "age_unit": "years",
  "gender": "M",
  "birth_weight_g": 3200,
  "primary_diagnosis": { "code": "I63.900", "name": "脑梗死" },
  "other_diagnoses": [
    { "code": "E11.900", "name": "2型糖尿病" },
    { "code": "I10.000", "name": "高血压病" },
    { "code": "J96.000", "name": "急性呼吸衰竭" }
  ],
  "primary_operation": { "code": "38.1201", "name": "颈动脉内膜剥脱术" },
  "other_operations": [
    { "code": "88.4100", "name": "脑血管造影" },
    { "code": "96.7201", "name": "有创呼吸机支持治疗" }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `patient_id` | string | 否 | 原样回显，便于对账 |
| `age` | number | **是** | 数值；配合 `age_unit` |
| `age_unit` | string | 否 | `years`（默认）/ `months` / `days`；也接受 岁/月/天 |
| `gender` | string | **是** | `M`/`F`；也接受 `1`/`2`、男/女 |
| `birth_weight_g` | number | **新生儿必填** | 出生（入院）体重，单位克；年龄 ≤28 天时必填，其他病例忽略 |
| `primary_diagnosis` | object | **是** | `{code,name}`，`name` 可空；也可直接写编码字符串 |
| `other_diagnoses` | array | 否 | 同上，可空 |
| `primary_operation` | object \| null | 否 | 主要手术/操作；没有就不传或传 `null` |
| `other_operations` | array | 否 | 其他手术/操作 |

**年龄口径**（对应方案里的 `NL` 周岁 / `XSRTL` 天龄）：

| 输入 | NL | XSRTL | 说明 |
|---|---|---|---|
| `age=65, age_unit=years` | 65 | 23725 | 成人 |
| `age=3, age_unit=months` | 0 | 91 | 婴儿 |
| `age=10, age_unit=days` | 0 | 10 | 新生儿（≤28 天需体重） |
| `age=0, age_unit=years` | — | — | **拒绝**：0 岁无法区分新生儿/婴儿，请改用 months/days |

**编码写法**：直接填医保版编码即可，大小写 `X`/`x` 自动等价；空格会被忽略。诊断带星号（如 `A01.002+G01*`）原样传入。

---

## 4. 输出格式

### 成功入组

```json
{
  "is_grouped": true,
  "grouping_result": {
    "patient_id": "345899002",
    "mdc":  { "code": "MDCB", "name": "神经系统疾病及功能障碍" },
    "adrg": { "code": "BE1",  "name": "颈及脑血管手术" },
    "drg":  { "code": "BE19", "name": "颈及脑血管手术" },
    "rw": "NAN"
  }
}
```

### 未入组 / 歧义组 / 参数不合法

```json
{
  "is_grouped": false,
  "grouping_result": {
    "patient_id": "C001",
    "mdc": null, "adrg": null, "drg": null,
    "rw": "NAN"
  },
  "reason": {
    "code": "NEWBORN_WEIGHT_MISSING",
    "message": "新生儿（天龄 10 天）未提供出生体重 birth_weight_g（单位：克），无法在早产儿/新生儿体重分档（PS1–PS5）中确定组别"
  }
}
```

- `rw` 固定为字符串 `"NAN"`（暂无分值表）；后续接入权重表时只需替换该字段。
- 未入组时 `mdc` 可能仍有值（已定位到 MDC 但没进 ADRG），便于排查。
- 歧义组（`AMBIGUOUS`）会给出 `adrg`（如 `GQY`）但 `drg` 为 `null`。
- 传 `?debug=1` 会额外返回 `diagnostics`：有效 MCC/CC、排除判定明细、分组轨迹、耗时。

### 原因码

| code | 含义 |
|---|---|
| `NOT_GROUPED` | 主诊断不在主诊表内，或落入 MDC 的 x00 兜底 |
| `AMBIGUOUS` | 命中歧义组（xQY）：主要手术在分组内涵列表，但主诊断所在 MDC 没有对应外科组 |
| `NEWBORN_WEIGHT_MISSING` | 年龄 ≤28 天但未提供 `birth_weight_g` |
| `MISSING_PRIMARY_DIAGNOSIS` | 缺主要诊断 |
| `INVALID_AGE` / `INVALID_GENDER` / `INVALID_INPUT` | 字段缺失或不合法 |

---

## 5. 分组口径

严格按配置表的排序执行五步：

1. **先期分组（MDCA）**：AA1–AH3（移植、ECMO、有创呼吸机≥96 小时等），命中即出组，不再看主诊断。
2. **MDC**：按 `MDC.排序` 逐条匹配（新生儿 → 多发严重创伤 → 主诊表）。
3. **MDC 内 ADRG**：按 `MDC内排序` 走；先外科/操作组，然后"有主要手术但没进外科组"落入 `xQY` 歧义组，最后 `x00` 兜底（未入组）。
4. **ADRG 内 DRG**：`MCC → CC → 不伴合并症`（规则为空的行为兜底）。
5. **CC/MCC 判定**：其他诊断命中 CC 表，且**主要诊断不在该 CC/MCC 的排除表**内才算有效。

### 与原《DRG分组查询_单机版.html》的三处差异

| 项 | 单机版 | 本分组器 | 影响 |
|---|---|---|---|
| `not in` 遇到空值 | 判为 False | 判为 True | 原版导致"29 天≤年龄＜1 岁、无主要手术"的婴儿病例永远进不了 PV1 组（实测 3 月龄病例显示"未检索到分组信息"）；本版已修正 |
| 新生儿体重 | 有输入框但可空跑 | ≤28 天无体重直接 `is_grouped=false` | 避免 PS1–PS5 被错误并入"不伴合并症"组 |
| 主要手术 | 可多选最多 10 条 | 单条 | 与结算清单"一条主要手术"口径一致 |

> 机器人辅助手术请把**实际术式**填 `primary_operation`（如 `32.4100`），把机器人编码（`17.4100` 等）放 `other_operations`：方案中的机器人细分组条件是"全部手术或操作包含机器人操作"，ADRG 仍由主要手术决定。

---

## 6. 数据与更新

- 数据源：`data/DRG3.0分组方案配置信息.xlsx`（MDC / ADRG / DRG / 集合 / CC / 排除表 六张表）。
- 首次启动解析 xlsx（约 4 秒）并写入 `data/config.cache.json.gz`；之后启动 <1 秒。
- 替换 xlsx 后启动会自动识别（比对大小与修改时间）并重建；也可 `python start.py --rebuild`（macOS/Linux 用 `python3`）手动重建。
- 缓存里只有分组方案本身，不含任何病历数据。

---

## 7. 性能与容量

- 单条分组：约 0.16 ms（含 CC/MCC 判定，实测 5000 条 794 ms）。
- 批量接口：约 5000 条/秒；1 万条约 2 秒（建议 1000 条/批）。
- 单次批量上限 50000 条、请求体上限 64 MB；建议每批 1000–5000 条，便于失败重试。

---

## 8. 安全与合规

- 默认只监听 `127.0.0.1`，同一局域网的其他机器访问不到（需要分享时再用 `--host 0.0.0.0`，并自行加防火墙约束）。
- 代码中没有任何外网请求；不写日志文件，不保存请求内容（控制台只打印方法、路径、状态码）。
- 测试页面的"服务地址"默认本机；改成别的地址才可能外发，请勿随意修改。
- 若要做内网多终端使用，建议放在科室内部服务器并只开放内网端口，同时保留本项目的"无外网出站"约束。

---

## 9. 测试

```bash
python tools/selftest.py       # 端到端：进程内起服务，验证版本/单条/批量/CSV/页面
node tools/test_csv_page.js    # 页面 CSV 解码/清洗：UTF-8、BOM、GBK、sep=、空行（需 Node）
python tests/test_engine.py    # 37 项：配置规模、典型病例、CC/MCC、排除、机器人、高危妊娠、歧义、未入组、年龄/性别写法、随机 300 例稳健性
python tests/test_api.py       # 34 项：健康检查、单条/批量/CSV、编码检索、模板下载、错误码、CORS、OPTIONS
```

Windows 命令提示符里把 `/` 换成 `\` 即可（`python tests\test_engine.py`）；PowerShell、macOS、Linux 直接用上面的写法。

两套均应输出"全部通过"。

---

## 10. 常见问题

**Q：双击 start.bat 闪退（Windows）？**
A：多半是没装 Python 或没加入 PATH。命令行执行 `python --version` 确认；安装 Python 时勾选 "Add python.exe to PATH"。

**Q：macOS 双击 start.command 没反应 / 提示 permission denied？**
A：先在终端执行一次 `chmod +x start.sh start.command`。如果系统提示"无法打开，因为它来自身份不明的开发者"，在"系统设置 → 隐私与安全性"里点"仍要打开"，或直接终端运行 `./start.sh`。

**Q：Linux 报 `./start.sh: /usr/bin/env: bad interpreter` 或 `$'\r': command not found`？**
A：文件被 Windows 工具改成了 CRLF 换行。执行 `sed -i 's/\r$//' start.sh start.command` 修正；本仓库交付的文件是 LF，复制时注意不要用会转换换行的工具。

**Q：Linux / 服务器上没有图形界面，启动时报浏览器相关错误？**
A：用 `python3 start.py --no-open`（或 `./start.sh --no-open`），分组服务照常监听 8765，用 curl 或业务系统直接调接口。

**Q：端口 8765 被占用？**
A：`python start.py --port 8766`（macOS/Linux 用 `python3`），同时把测试页右上角"服务地址"改成 `http://127.0.0.1:8766`。查占用进程：Windows `netstat -ano | findstr :8765`，macOS/Linux `lsof -i :8765` 或 `ss -lptn 'sport = :8765'`。

**Q：测试页提示"无法连接分组服务"？**
A：先确认服务窗口还开着；个别浏览器会拦截 `file://` 页面访问本机端口，此时改用 <http://127.0.0.1:8765/> 打开。

'**Q：上传 CSV 后没有结果？**
A：页面会自动识别 UTF-8 / 带 BOM / GBK 三种编码，并自动去掉 Excel 可能写入的 `sep=,` 首行和开头空行；上传后卡片下方会显示编码、行数和分组条数。若提示"首行没有逗号"，说明文件是分号分隔，请在 Excel 里用「另存为 → CSV（逗号分隔）」重新导出。结果出来后点「下载结果 CSV」即可保存。

**Q：为什么病例显示"歧义组"？**'
A：主诊断所在 MDC 与主要手术不匹配（如主诊断是阑尾炎、主要手术填了肺叶切除）。请核对主要手术是否填错，或主诊断选择是否符合主要诊断选择原则。

**Q：为什么"未入组"？**
A：常见三种：诊断编码不在 3.0 主诊表（旧版编码/自定义编码）；主要手术编码不在该 MDC 的外科组内涵；其他诊断不足以支撑进入任何 ADRG。可用 `?debug=1` 看轨迹。

**Q：结果和本地医保平台的分组不一致？**
A：先核对输入口径（主要手术取值、其他诊断是否齐全、新生儿体重、年龄单位），再核对平台使用的方案版本。本分组器严格按配置表规则执行；若仍不一致，请把病例（脱敏）和 `?debug=1` 的轨迹一起反馈。

---

## 11. 免责声明

本工具用于**分组试算、编码核对与培训**，不替代医保经办机构的正式分组与结算结果；`rw` 暂无分值表，统一输出 `NAN`，不参与任何费用计算。使用前请确认配置版本与本地执行的 3.0 版方案一致。
