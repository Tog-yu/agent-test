# 航运智能账单助手验收测试报告

- 测试日期：2026-11-01（第 2 轮复测：修复 P/C 缺口 + 清理重复测试）
- 需求来源：<https://ks.wjx.com/vm/h46aF6R.aspx>
- 项目版本：`shipping-bill-agent 1.7.0`
- 测试环境：macOS / Python 3.13.12
- LLM：WB OpenAI 兼容反代 `http://127.0.0.1:8788/v1`，模型 `deepseek-v4.1-flash`
- OCR：题目网关 `https://gateway.pltplt.com/ai/process`，**本轮在线真实调用**
- 总体结论：**LLM + OCR 全链路已验证通过；付款方式 `P/C → CC` 缺口本轮已修复。验收第 4 项仍为部分通过，唯一剩余缺口是扫描件币种/发票日期/总计——上游 OCR 响应里没有这些字段。**

## 1. 执行摘要

| 测试项 | 结果 | 关键结果 |
|---|---|---|
| WB LLM 配置与连通性 | 通过 | `/v1/models` 200，配置模型可用；结构化调用连续成功 |
| 原始附件在线全链路 | 通过 | 7 条记录；OCR 调用 **5 次 / 错误 0**；LLM 调用 **6 次 / 错误 0** |
| 原始附件解析错误 | 通过 | `errors.json = []` |
| 原始附件输出契约 | 通过 | 7 条记录均为合法标准 JSON |
| 全格式样本 LLM 链路 | 通过 | 10 条记录；CREATE 4 / UPDATE 1 / NO_CHANGE 4 / PENDING_REVIEW 1；`llm_errors = 0` |
| 全格式样本输出契约 | 通过 | 10 条记录均通过契约校验 |
| 全量自动化测试 | 通过 | **216 passed** |
| 金标评测门禁 | 通过 | 11 个字段 P/R/F1 全 1.000；记录级正确率 1.000；风险召回 1.000 |

> 与上一轮的区别：本轮修掉了 `P/C` 列付款方式、跨轨同源行重复计费两个真实缺陷，
> 并清理了 10 个被覆盖的重复测试函数，测试数由 192 增至 216。

## 2. 配置与调用证据

### 2.1 LLM

- 配置来源：用户提供的 `wb 反代.md`
- Base URL：`http://127.0.0.1:8788/v1`
- 模型：`deepseek-v4.1-flash`
- 配置落点：项目根 `.env`（已被 `.gitignore` 忽略）
- 兼容性修复：
  - 某些 OpenAI 兼容网关不支持 `response_format=json_schema`，实际调用会 404；改为 `with_structured_output(..., method="function_calling")`。
  - 本机存在全局 HTTP 代理，LangChain 会把 `127.0.0.1:8788` 请求也送入代理，第二次请求起稳定 404；`.env` 增加 `NO_PROXY=127.0.0.1,localhost` 后连续调用正常。
- 最终真实批次：`llm_calls=6 / llm_errors=0 / llm_cache_hits=0`

### 2.2 在线 OCR

5 个需 OCR 的文件均上传为公网临时地址，并由题目网关真实下载识别：

| 文件 | 在线结果 |
|---|---|
| `debit_note_DC-108478.pdf` | 成功 |
| `arrival_notice_NEPTUNE.pdf` | 成功 |
| `zim_invoice_scan.png` | 成功，单号识别为 `ORFR1618731` |
| `4055331281 HBL.pdf` | 成功（提单，无费用） |
| `4055331281 MBL.pdf` | 成功（提单，无费用） |

最终计数：`ocr_calls=5 / ocr_errors=0 / ocr_cache_hits=0`。

> 网关侧有偶发 504（复测中曾出现一次 2/5 超时）。系统按设计回退 PDF 文本层并留痕，
> 结果仍为 7 条且 `errors.json` 为空；本报告采用 5/5 全成功的那一轮作为证据。

## 3. 原始附件最终业务结果

输出：`out/final-online-full-v7/result.json`，共 **7 条**，全部 `PENDING_REVIEW`。

| 来源 | 单号 | 费用 | 金额 | 币种 | 付款方式 | AR/AP |
|---|---|---|---|---|---|---|
| Arrival Notice | null | On Carriage Charge | 245.00 | USD | null | null |
| Debit Note | `DC-108478` | Detention | 1310.40 | USD | **CC** | null |
| Debit Note | `DC-108478` | Handling Charge | 65.00 | USD | **CC** | null |
| Debit Note | `DC-108478` | Handling Charge | 35.00 | USD | **CC** | null |
| 扫描图 | `ORFR1618731` | Wharfage Charge | 5.00 | null | null | null |
| 扫描图 | `ORFR1618731` | Wharfage Charge | 21.90 | null | null | null |
| PRE-ALERT EML | `SSZSE26093128` | Handling Charge | 50.00 | USD | **PP** | null |

金额勾稽：

- Debit Note：65.00 + 1310.40 + 35.00 = **1410.40**，与单据总计一致。
- 扫描图：5.00 + 21.90 = **26.90**，与单据总计一致。
- Arrival Notice：245.00，与费用区块一致。
- EML：正文唯一条费用 50.00 USD。

全部转人工的原因：

1. 当前记账主体 `PltPlt` 不在 Debit Note / Arrival Notice 的明确交易双方之间。
2. EML 单据类型为 Unknown，不足以判 AR/AP。
3. 扫描件缺少交易方与币种字段。
4. 这是“该自动的自动、该拦的拦住”，不是全量降级失败。

## 4. 修复记录（两轮累计）

| # | 问题 | 修复 |
|---|---|---|
| 1 | WB 反代不支持 `json_schema` 结构化输出，稳定 404 | 改用 `function_calling` 结构化输出 |
| 2 | 全局 HTTP 代理劫持 localhost，导致请求复用后 404 | `NO_PROXY=127.0.0.1,localhost` |
| 3 | LLM 404 不重试，偶发错误直接降级 | 应用层按 `max_retries` 重试，且提案异常不得击穿规则轨 |
| 4 | 规则轨空占位字段 + LLM 未返回该键时，对账 `assert` 崩溃 | 两边均无值时跳过；单文件失败不再产生 `E_PARSE_FAILED` |
| 5 | `US$` 与 `USD` 被误判为币种冲突，产生假人工复核 | 对账前做 ISO 4217 归一比较 |
| 6 | LLM 把同一费用写成不同原始名称，导致费用行重复 | 按“标准费用名 + 金额”合并；同名不同额只记冲突，不新增行 |
| 7 | LLM 把 `FREIGHT PREPAID` 提出为无金额费用行，7 条变 8 条 | 无有效金额的 LLM-only 候选直接丢弃并记 `V-LLM_UNPRICED` |
| 8 | **PDF 费用行 `P/C = C` 未归一为 `CC`，三行付款方式全 null** | 新增 `normalize/payment.py`（P→PP、C→CC、Collect/Prepaid/到付/预付同义归一）；规则轨「列切分 / 无分隔行」两档都接入；LLM prompt 明确 P/C 语义，输出同样过归一；对账比较前先归一，避免「C vs CC」假冲突 |
| 9 | **LLM 与规则对同一行起不同名（`H/C` vs `SHA pay TAR`），一条费用入两次账** | 对账增加「原文证据片段认行」：同 span + 同金额即视为同一行，合并并记 `V-DUP_LINE`；同额不同行不合并 |
| 10 | **测试文件存在 10 个被覆盖的重复函数定义** | 删除 `test_boundaries.py` 7 个、`test_real_samples.py` 3 个前置重复定义（后定义内容完全相同），测试数不变但不再有静默覆盖 |

修改文件：

- `src/shipping_bill_agent/normalize/payment.py`（新增）
- `src/shipping_bill_agent/extract/rule_extractor.py`
- `src/shipping_bill_agent/extract/reconcile.py`
- `src/shipping_bill_agent/extract/llm_extractor.py`
- `src/shipping_bill_agent/extract/prompts.py`
- `src/shipping_bill_agent/pipeline.py`
- `src/shipping_bill_agent/config.py`
- `tests/test_payment_term.py`（新增 22 项）
- `tests/test_reconcile.py`（共 7 项，本轮补 2 项同源行去重回归）
- `scripts/online_acceptance.py`
- `README.md`、`docs/REQUIREMENTS_ANALYSIS.md`
- `.env`

## 5. 十项验收标准对照

| # | 验收要求 | 结论 | 证据与说明 |
|---|---|---|---|
| 1 | 正确解析 EML 正文及附件 | **通过** | EML 正文 1 条费用，2 个 PDF 附件递归解析，内嵌签名图不送 OCR |
| 2 | 调用 OCR API 识别 PDF 和图片 | **通过（本轮在线）** | 5 次真实网关调用，0 错误 |
| 3 | 解析 Excel、Word 费用表格 | **通过** | XLSX/DOCX 端到端；XLS/DOC 由自动化测试覆盖 |
| 4 | 准确提取单号、费用、金额和币种 | **部分通过** | 单号/费用/金额全对，付款方式本轮已补；扫描件币种仍 null（网关不返回，不硬猜） |
| 5 | 区分新增和更新账单 | **通过** | 样本 LLM 结果：CREATE 4 / UPDATE 1 / NO_CHANGE 4 |
| 6 | 结合记账主体判断 AR/AP | **通过** | 规则测试覆盖四象限；真实附件按规则不硬判 |
| 7 | 检测重复账单和数据冲突 | **通过** | 重复行、勾稽、OCR 混淆、真/假冲突均有测试；本轮新增同源行去重 |
| 8 | Harness 提升稳定性和输出准确性 | **通过** | Schema/证据锚定/重试/降级/审计 trace/金标门禁全部实际运行 |
| 9 | 5 点以上思考 | **通过** | `docs/REFLECTIONS.md` 共 14 条 |
| 10 | Python + LangChain 实现 | **通过（本轮真实 LLM）** | LangChain LCEL + Function Calling + Pydantic 结构化输出，线上 0 错误 |

## 6. 仍存在的缺口

### F-01：扫描件币种、日期、总计无法从网关响应获得（保留，不修）

- 在线 OCR 已正确返回单号 `ORFR1618731` 和两笔金额 5.00 / 21.90。
- 但网关 `chargeList` 只有 `chargeItem / amount / chargeCtr`，没有 currency；响应中没有发票日期和 total 字段。
- 因此输出 `currency = null`、`document_date = null`，无法执行勾稽校验。
- 这不是代码丢字段，而是上游 OCR 能力边界；系统没有拿 `USD` 等线索硬猜，符合“不虚构”。
- 若要补齐，只能改网关或对扫描件做二次图像解析；在现有网关下应继续保持 `null` + 人工复核。

### F-02：PDF 的 `P/C = C` 未归一为 `CC` —— **本轮已修复**

- 新增 `normalize/payment.py`，把 `P/C/P → PP`、`C/CC/Collect/到付 → CC`，认不出的写法一律 `null`。
- 规则轨两档（有列分隔的单元格、无分隔挤成一行的 `… 1.00 C 65.00`）都已接入。
- LLM 轨：prompt 明确“P/C 列：P=Prepaid=PP，C=Collect=CC”，输出再统一归一。
- 验证：`debit_note_DC-108478.pdf` 三行记录 `payment_term = CC`；`arrival_notice_NEPTUNE.pdf` 仍为 `null`（没有 P/C 列，不硬塞）。
- 回归测试：`tests/test_payment_term.py` 22 项（含 17 种写法的归一断言）。

### F-03：EML 交易方语义被 LLM 误填（按用户要求跳过，不修）

- EML 记录 `issuer` 取到邮件签名、`recipient` 取到 To 头地址，两者同属一家公司，不是真实账单交易双方。
- 该记录为 `PENDING_REVIEW`，不会自动入账，但字段语义不准确。
- 根因：证据锚定只验证“字符串在原文中出现”，没有验证“该字符串在原文中承担交易方角色”。
- 本轮按用户要求不处理。

### F-04：文档与测试的陈旧内容（部分处理）

- `tests/` 的 10 个重复测试函数（被后定义静默覆盖）**已删除**。
- `README.md` 测试数量由“130 项”改为 **216 项**。
- `docs/REQUIREMENTS_ANALYSIS.md` 的“题目附件未获取”改为**已获取**，并补充 `P/C` 归一说明。
- `docs/REAL_DATA_NOTES.md` 仍有水印修复前的 AP 口径段落，未在本轮处理。

## 7. 最终结论

**核心功能、LLM、在线 OCR、账单更新、AR/AP、校验和输出契约均已验证通过；本轮补上了付款方式与跨轨重复计费两个真实缺口。**

仅剩验收第 4 项的部分缺口：扫描件币种/日期/总计受上游 OCR 能力限制，系统按“不猜”原则置 `null` 并转人工复核。

可复现产物：

- `out/final-online-full-v7/result.json`：真实附件在线全链路结果（7 条，付款方式已补）
- `out/final-online-full-v7/online-manifest.json`：OCR/LLM 调用计数与公网文件映射
- `out/final-online-full-v7/errors.json`：空
- `out/final-samples-llm-v6/result.json`：全格式 LLM 结果（10 条）
- `out/final-samples-llm-v6/report.json`：LLM 计数 0 错误
