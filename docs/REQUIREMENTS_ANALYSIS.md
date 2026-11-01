# 需求分析：多格式文档解析的航运智能账单助手智能体

> 版本：1.0.1 · 日期：2026-10-31
> 需求来源：笔试题目页面 `https://ks.wjx.com/vm/h46aF6R.aspx`（AI agent 笔试题）
> 关联文档：`docs/DESIGN.md`（技术方案）、`docs/DEV_SPEC.md`（施工规格）、`docs/REFLECTIONS.md`（思考补充）

---

## 0. 事实与假设的边界（先说清楚，避免后文被误读）

| 项 | 状态 | 说明 |
|---|---|---|
| 笔试题正文 | **已获取** | 2026-10-31 抓取到完整题目正文，本文所有需求条目均来自该页面 |
| 题目附件数据（百度网盘 `pan.baidu.com/s/1ihoJqkhlJdrqec86Pf4_2Q`） | **已获取**（2026-11-01） | 原始附件已落到 `笔试题AIAGENT/`，真实单据镜像在 `data/real/`（Debit Note / Arrival Notice / PRE-ALERT 邮件 / 扫描件），`scripts/make_samples.py` 生成的合成样本仍保留用于格式覆盖 |
| OCR 网关 `https://gateway.pltplt.com/ai/process` | **已真机调通**（2026-11-01） | 公网 PDF 地址 → `HTTP 200 / success:true / 7–12s`。约束：`file` 必须网关可下载；给无效地址时服务端卡在下载导致超时（曾误判为网络不通）。详见 DEV_SPEC §8.5 |
| 记账主体、真实费用口径、真实单据模板 | **未获取** | 题目未给。所有业务规则按题目文字 + 航运/货代行业通行做法推断，均在 `docs/DESIGN.md §12 假设清单` 逐条登记 |

> 结论：**能被验证的部分**（解析、归一化、匹配、AR/AP、校验、输出契约）走真实代码 + 单元测试；网关 OCR 客户端已真机调通，但「真实账单 → OCR → 抽取」整链受限于公网 `file` 地址，本地默认走 `cassette` 回放。

---

## 1. 需求条目化（原文 → 可验收条目）

### 1.1 输入能力（题目二.1、三）

| ID | 需求原文 | 可验收定义 |
|---|---|---|
| R-IN-1 | 支持 EML、PDF、图片、Excel、DOC/DOCX 等数据源 | 单一入口 `run(path)` 能按扩展名/MIME 分派到对应解析器，未知类型抛结构化错误而非崩溃 |
| R-IN-2 | 自动解析 EML 邮件正文、邮件头及附件 | 提取 Subject/From/To/Cc/Date + text/plain 正文（无则退化 html→text）+ 附件递归解析；嵌套 EML（`message/rfc822`）支持多层 |
| R-IN-3 | PDF 或图片通过指定 OCR API 识别 | PDF/PNG/JPG/TIFF 均走 OCR 适配器；请求体严格 `{file, task, callBack, taskType}`；`file` 为 OCR 侧可访问的 URL |
| R-IN-4 | 已存在的账单 JSON，用于匹配和更新 | `ExistingBillStore` 从 JSON 加载历史账单，支持按四级键检索 |

### 1.2 解析与提取（题目二.4、四.1、四.2）

| ID | 需求 | 可验收定义 |
|---|---|---|
| R-EX-1 | 单据类型识别 | 输出枚举：`Debit Note` / `Credit Note` / `Invoice` / `Unknown`，命中依据留痕 |
| R-EX-2 | 单据编号与日期 | `reference_no`、`document_date`（`YYYY-MM-DD`），支持 `DN-202609-001`、`INV/2026/0042`、`2026年9月20日` 等写法 |
| R-EX-3 | 签发方与接收方 | `issuer`、`recipient` 字符串；取自 `Issued by / To / Bill To / Shipper / Consignee` 等标签 |
| R-EX-4 | 费用名称 | 原始名 + 标准名双写 |
| R-EX-5 | 数量、单价、金额 | 三者尽量全取；只有金额时 qty/unit_price 置 `null` |
| R-EX-6 | 币种 | 三位代码（ISO 4217）；符号/单词先归一化 |
| R-EX-7 | 付款方式 PP/CC | `PP`（Prepaid）/`CC`（Collect）；费用表 `P/C` 列同样归一（P→PP、C→CC）；未标注则 `null`，**不猜** |
| R-EX-8 | 小计、税额、总计 | 独立字段保留，用于勾稽校验 |

### 1.3 归一化（题目四.3）

| ID | 需求 | 可验收定义 |
|---|---|---|
| R-NM-1 | 费用名标准化 | 内置别名表 + 规则化降级（大小写/去标点/去复数/关键词），至少覆盖题目 6 组映射 |
| R-NM-2 | 保留原始名 | 输出同时带 `charge_item_raw` |
| R-NM-3 | 币种标准化 | ISO 4217 白名单校验；`US$`/`USD`/`美金`/`$`→`USD`；非法币种不静默改，标风险 |

### 1.4 账单操作（题目四.4、四.5、六）

| ID | 需求 | 可验收定义 |
|---|---|---|
| R-OP-1 | 新增账单 | 无匹配 → `CREATE`，每个费用项一条记录 |
| R-OP-2 | 更新已有账单 | 命中匹配 → `UPDATE`，只改**输入材料明确变更**的字段，其余继承原值 |
| R-OP-3 | 其他操作态 | `NO_CHANGE`（命中且无变更）、`PENDING_REVIEW`（低置信/冲突/校验失败） |
| R-OP-4 | 四级匹配 | ①单据编号+费用名 ②提单号+费用名+币种 ③订舱号+费用名+币种 ④箱号+交易双方+日期+金额；按优先级短路，命中多条→升级人工 |
| R-OP-5 | 一费一条 | `charge_details` 一次承载一个费用项，多费用 = 多条记录 |

### 1.5 AR/AP（题目四.6）

| ID | 需求 | 可验收定义 |
|---|---|---|
| R-FI-1 | 记账主体视角判断 | 输入 `accounting_entity`；不得仅凭 DN/CN 判 AR/AP |
| R-FI-2 | 规则可解释 | 判定链：`issuer == entity` → 我方对外收费 → `AR`（DN）/`AP`（CN 冲销）；`recipient == entity` → 对方对我收费 → `AP`（DN）/`AR`（CN 冲销）；主体不在双方 → `PENDING_REVIEW` |

### 1.6 校验与质量（题目四.7、七）

| ID | 需求 | 可验收定义 |
|---|---|---|
| R-VD-1 | 金额有效性 | 非法数字/负数/超精度 → 记 issue，降级 `PENDING_REVIEW` |
| R-VD-2 | 币种合规 | 非 ISO 4217 → issue |
| R-VD-3 | 明细与合计一致 | `sum(detail) + tax == total`，容许 ±0.01 舍入；不一致 → 冲突 issue |
| R-VD-4 | OCR 混淆检测 | `0/O`、`1/I/l`、`5/S`、`8/B`、`6/G` 等；**金额/单号命中只标记不自改** |
| R-VD-5 | 重复提取检测 | 同单同费用同金额多条 → 去重 + issue |
| R-VD-6 | 重复账单检测 | 跨文件/与历史账单同号同费用同金额 → issue |

### 1.7 输出与工程要求（题目五、七、八.8-10）

| ID | 需求 | 可验收定义 |
|---|---|---|
| R-OT-1 | 标准 JSON 数组 | 顶层 list；键名与题目一致；`json.loads` 可解析；`json.dumps(ensure_ascii=False)` |
| R-OT-2 | 不虚构 | 缺字段填 `null`，不从常识补全；LLM 输出需过"证据锚定"校验 |
| R-OT-3 | 金额为数字类型 | `float`/`int`，禁字符串 |
| R-OT-4 | 审计留痕 | `audit_trail.ai_reasoning` 为字符串数组；另出 `report.json` 带字段级 provenance |
| R-OT-5 | 必须 Python + LangChain | 依赖与 import 可见；LLM 链路用 LangChain LCEL + 结构化输出 |
| R-OT-6 | Harness 工程 | 见 `docs/DESIGN.md §9`：提案-校验分离、磁带回放、schema 重试、置信度路由、评测集 |
| R-OT-7 | 5 点以上思考 | `docs/REFLECTIONS.md`（12 条） |

---

## 2. 需求中的歧义与我的取舍

| 歧义点 | 题目说法 | 取舍 | 理由 |
|---|---|---|---|
| PDF 是否一律走 OCR | "PDF 或图片通过指定 OCR API 识别" | 默认 OCR；提供 `--pdf-strategy auto` 允许文本层可用时跳过 OCR | 网关 300s 超时是瓶颈；文本层 PDF 走 OCR 属浪费且引入不必要错误。默认仍按题意走 OCR，保证合规 |
| `file` 参数怎么来 | "应为 OCR 服务端能够访问的下载地址" | 三档：`gateway+public-url`（生产）/`gateway+upload`（可选对象存储）/`cassette`（离线） | 本地 mock 与真实链路的差异必须显式化，不能假装本地文件能直传 |
| 原始费用名放哪 | 要求"同时保存"，但标准输出格式无此字段 | 输出加**扩展字段** `charge_details.charge_item_raw`，并在 `--strict-schema` 下剥离 | 既满足业务要求，又不破坏验收契约 |
| 记账主体哪来 | 题目未说明入参 | CLI/API 入参 `--entity`，缺省读配置；未提供时 `posting_type=null` + `PENDING_REVIEW` | AR/AP 必须有主体视角，无主体不可判 |
| 一张单据多个费用是否要合计校验 | 未明确 | 单据级勾稽，费用项各自出记录 | 合计只可能落在单据层 |
| `audit_trail.ai_reasoning` 内容粒度 | 未明确 | 每条记录 3-6 句，覆盖：识别依据 / 匹配依据 / 变更依据 / 未改字段理由 / 风险提示 | 对齐题目示例的三句式，且可回溯 |
| `document_type` 是否只有 DN/CN/Invoice | 「六、字段说明」只约束了 `operation`，未给 `document_type` 闭集 | 增加 `Arrival Notice`（真实数据里 CMA CGM 的到货通知含应付费用，DN/Invoice 都不贴切）；AR/AP 里与 DN 同归「正向计费」，理由写在 `ar_ap.py` | 硬塞进 Debit Note 属于分类错误；改回四值是改一行常量的事 |

---

## 3. 非功能要求（题目未写，我按财务系统常识补）

| 维度 | 目标 |
|---|---|
| 确定性 | 同一输入 + 同一配置 → 同一输出（LLM 关闭时 100%；LLM 开启时通过 `temperature=0` + 缓存保证可重放） |
| 幂等性 | 重复投递邮件不产生重复账单：`(file_sha256, reference_no, charge_item, amount, currency)` 幂等键 |
| 可观测 | 每次运行产出 `trace-<run_id>.json`：阶段耗时、OCR 调用、LLM 调用、token、决策点 |
| 可评测 | `tests/fixtures` 标注金标，跑出字段级 precision/recall（见 §9 评测） |
| 健壮性 | 单文件解析失败不影响批次其他文件；失败产出 `PENDING_REVIEW` + error 记录 |
| 安全 | EML 附件不落盘执行、不解析宏；HTML 正文不执行脚本；LLM prompt 注入用分隔符 + 指令约束 |

---

## 4. 验收清单（对题目"八、验收标准"逐条落点）

| 题目条目 | 落点 |
|---|---|
| 1 解析 EML 正文及附件 | `src/shipping_bill_agent/ingest/eml_parser.py` + `tests/test_ingest.py::test_parse_eml_headers_and_attachments` |
| 2 调 OCR 识别 PDF/图片 | `ocr/client.py`、`ocr/providers.py` + `tests/test_ocr.py` |
| 3 解析 Excel/Word 费用表格 | `ingest/excel_parser.py`、`ingest/word_parser.py` + `tests/test_ingest.py::test_parse_excel_tables`、`::test_parse_word_table` |
| 4 准确提取单号/费用/金额/币种 | `extract/rule_extractor.py` + `extract/llm_extractor.py` + `tests/test_extract.py` |
| 5 区分新增/更新 | `matching/matcher.py`、`decide/planner.py` + `tests/test_matching.py` |
| 6 结合记账主体判 AR/AP | `decide/ar_ap.py` + `tests/test_ar_ap.py`（正反四个象限） |
| 7 检测重复账单和冲突 | `validate/validators.py` + `tests/test_validate.py` |
| 8 Harness 设计 | `docs/DESIGN.md §9` + 实际代码（重试/回放/校验闸门/评测脚本） |
| 9 5 点以上思考 | `docs/REFLECTIONS.md`（12 条） |
| 10 Python + LangChain 实现 | `requirements.txt`、`pyproject.toml`、`extract/llm_extractor.py` |
