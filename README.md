# 航运智能账单助手智能体（Shipping Bill Agent）

> 笔试题实现：**多格式文档解析的航运智能账单助手智能体**
> 技术栈硬要求：Python + LangChain　·　交付形态：可运行代码 + 规格文档 + 测试 + 评测

多格式输入（EML / PDF / 图片 / Excel / Word）→ OCR 与结构化抽取 → 费用名·币种·金额归一 → 五道校验闸 → 四级账单匹配 → CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW 决策 + AR/AP → 标准 JSON + 审计旁路产物。

---

## 1. 5 分钟跑通

```bash
cd ShippingBillAgent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 生成合成样本、历史账单与 OCR 回放磁带（仓库已带，重跑可覆盖）
python scripts/make_samples.py

# 跑主样本（离线 OCR 回放，无需网络与 API Key）
python -m shipping_bill_agent run \
  --input data/samples \
  --bills data/existing_bills.json \
  --entity "Origin Agent" \
  --out out/demo

# 校验输出是否符合题目「五、标准输出格式」
python -m shipping_bill_agent check --input out/demo/result.json --allow-extensions

# 测试 + 评测门禁
pytest
python scripts/evaluate.py --gate
```

产物：

| 文件 | 说明 |
|---|---|
| `out/demo/result.json` | **交付物**：题目要求的标准 JSON 数组 |
| `out/demo/report.json` | 每条记录的完整内部对象（字段级 provenance / issues / 匹配候选 / 降级标记） |
| `out/demo/trace-<run_id>.json` | 阶段耗时、OCR/LLM 调用计数、逐文件结果（含 `sha256` 与 `raw_snapshots`） |
| `out/demo/errors.json` | 解析失败清单（含错误码） |
| `out/demo/raw/<sha8>__<名>.txt` | **原文快照**：抽取时实际使用的文本，带来源/sha256/取自文本层还是 OCR 的头部。`report.json` 里每个 `provenance.span` 都能在这里检索到。`SBA_KEEP_RAW_TEXT=false` 可关 |

审计链怎么串起来（这四份文件的分工）：

```
trace.files[].path + sha256      ← 这份记录来自哪个文件（内容指纹，防篡改）
        ↓
out/demo/raw/<sha8>__*.txt       ← 该文件抽取时用的完整文本
        ↓
report.json  provenance.<field>  ← 字段级：span（原文片段）+ page/line_no + extractor + confidence
        ↓
report.json  issues[]            ← 这一条为什么被拦/被标疑（哪个闸门、什么严重级）
        ↓
result.json  audit_trail.ai_reasoning ← 人读的成段说明（识别依据→匹配依据→变更依据→风险）
```

### 1.1 接真实 OCR 网关

```bash
export SBA_OCR_MODE=gateway
export SBA_OCR_PUBLIC_BASE=https://your-bucket.example.com/bills   # 网关要能下载到文件
python -m shipping_bill_agent run --input data/samples --entity "Origin Agent"
```

题面给的接口要求 `file` 是**OCR 服务端可访问的下载地址**，本地路径不能直传；因此 gateway 模式必须配 `SBA_OCR_PUBLIC_BASE`，否则会明确报错而不是静默失败。

PDF 默认策略是 **`ocr`**：PDF 和图片都送 OCR 网关（对应题目「PDF 和图片通过以下 OCR API 识别」）。
网关不可达或返回空时**自动回退 PDF 文本层**，trace 里会写明「结果来自 PDF 文本层，非 OCR」；
只有连文本层都没有的扫描件才会进 `errors.json`。想省 OCR 调用就用 `--pdf-strategy auto`。

### 1.2 OCR 客户端与题面契约的对应关系

题面给的调用代码与实现逐一对应（`src/shipping_bill_agent/ocr/client.py`）：

| 题面 | 实现 | 说明 |
|---|---|---|
| `url = "https://gateway.pltplt.com/ai/process"` | `settings.ocr.url`，默认同值 | `SBA_OCR_URL` 可覆盖 |
| `data = {file, task, callBack, taskType}` | `build_payload()` 返回顺序完全相同的四个键，`callBack`/`taskType` 恒为 `""` | 本地 mock 网关实测收到的键与顺序一致 |
| `task = "orderChange"` | `settings.ocr.task`，默认 `orderChange` | `SBA_OCR_TASK` 可覆盖 |
| `requests.post(url, json=data, timeout=(10, 300))` | 同一句，`timeout=settings.ocr.timeout=(10.0, 300.0)` | 连接 10s / 读取 300s |
| `response.raise_for_status()` | 用同一个方法；5xx/429 交给重试，4xx 直接判契约错误 | 4xx 重试无意义 |
| `ocr_result = response.json()` | 校验顶层是对象、非 JSON 明确报 `E_OCR_FAILED` | 不假设响应内部形状 |
| `file_url` | `SBA_OCR_PUBLIC_BASE/<文件名>` | 题面要求 `file` 是**网关能下载到的地址**，本地路径传不了；未配置时明确报错而不是传本地路径 |

### 1.3 跑真实数据（题目网盘附件）

`data/real/` 里是题目网盘给的 4 份原始文件（1 EML + 2 PDF + 1 PNG）。记账主体是我方公司，别名用于识别"我方是否出现在单据上"：

```bash
python -m shipping_bill_agent run --input data/real \
  --entity "PltPlt" --entity-alias "湃乐多" --entity-alias "Pilot Aiship" --entity-alias "PLTPLT" \
  --out out/real
```

结果：**7 条记录，全部 `PENDING_REVIEW`**，`errors.json` 为空。
- Debit Note 3 条：65 + 1310.40 + 35 = 1410.40，与 `TOTAL AMOUNT` 完全勾稽。**AR/AP 判为 null**：这 3 张单的交易方是 NEPTUNE ↔ AEBO/YIWU，PltPlt 只以页脚 `Powered by` 水印出现，不在交易链上（见下方「AR/AP 为什么不判」）
- 扫描件 2 条：5.00 + 21.90 = 26.90，来自真实网关 OCR
改造过程与每处真实版式差异的修法见 **`docs/REAL_DATA_NOTES.md`**。

> 扫描件 `zim_invoice_scan.png` 的 OCR 结果来自**真实网关**（2026-11-01 真机取得，响应存为 `data/ocr_cassettes/f3ca6432d5597b72__orderChange.json`），不是伪造的磁带。详见 `docs/REAL_DATA_NOTES.md` §5。

### 1.4 启用 LLM（可选，增强语义字段）

```bash
cp .env.example .env    # 填 SBA_LLM_API_KEY
python -m shipping_bill_agent run --input data/samples --llm on --ocr-mode cassette
```

无 Key 时自动降级为纯规则轨，`report.json` 里 `degraded: true`，并在 trace 里写明原因——**不静默降级**。

---

## 2. 结果长什么样

`data/samples` 下 5 份样本 + 1 份旧格式文件，跑出来 10 条记录：

| 单据 | 费用项 | 金额 | 操作 | AR/AP | 判定依据 |
|---|---|---|---|---|---|
| DN-202609-001 (PDF) | Handling Charge | 40.00 | **UPDATE** | AP | T1 命中 B-1001（原 35.00），材料显式给出新金额 |
| DN-202609-001 | Terminal Handling Charge | 120.00 | NO_CHANGE | AP | T1 命中且金额一致 |
| DN-202609-001 | Documentation Fee | 35.00 | NO_CHANGE | AP | T1 命中且金额一致 |
| INV-202609-050 (XLSX) | Trucking Charge | 300.00 | NO_CHANGE | AP | T1 命中 B-1004 |
| INV-202609-050 | Customs Clearance Fee | 80.00 | CREATE | AP | 四级匹配零命中 |
| INV-202609-050 | Storage Charge | 25.00 | CREATE | AP | 四级匹配零命中 |
| DN-202609-002 (DOCX) | Handling Charge | 50.00 | NO_CHANGE | **AR** | 主体是签发方 → 对外收费 |
| DN-202609-002 | Trucking Charge | 280.00 | CREATE | AR | 匹配零命中 |
| CN-202609-007 (PNG/OCR) | Terminal Handling Charge | 15.00 | CREATE | AP | Credit Note + 主体是签发方 → mirror 口径冲销 |
| DN-202609-003 (PNG/OCR) | Handling Charge | 4.00 | **PENDING_REVIEW** | AP | OCR 把 `40.00` 认成 `4O.OO`，且明细与总计不勾稽 |

`data/samples/legacy_rates.xls` 是旧版二进制格式，**不崩**，进 `errors.json`：`E_UNSUPPORTED_XLS`。

---

## 3. 架构：8 段确定性管线，LLM 只做提案

```text
1 INGEST    扩展名+魔数分派；EML 正文与附件分开处理（附件递归，内嵌图不送 OCR）
2 OCR       PDF/图片 → 网关（cassette 回放 / gateway 真实 / stub 空）；auto 策略下有文本层的 PDF 不走 OCR
3 EXTRACT   规则抽取 ∥ LLM 提案 → 字段级对账            ← 唯一用 LLM 的一段
4 NORMALIZE 费用名别名表 / 币种 ISO 4217 / 金额 / PP·CC
5 VALIDATE  G1 金额 · G2 币种 · G3 勾稽 · G4 OCR 混淆 · G5 重复
6 MATCH     T1 单号+费用 → T2 提单号 → T3 订舱号 → T4 箱号+双方+日期+金额
7 DECIDE    CREATE / UPDATE（字段级 patch）/ NO_CHANGE / PENDING_REVIEW + AR·AP
8 EMIT      契约 JSON + report.json + trace + errors
```

### 3.1 一封邮件里三样东西要分开对待

| 部分 | 处理 | 理由 |
|---|---|---|
| 正文 | 当一张「无版式单据」抽字段与费用 | PRE-ALERT 这类邮件本身就是费用来源（如 `SHA pay TAR：H/C $50`） |
| 附件 | 递归解析成子文档，独立成单或独立判「非计费单据」 | 提单没有费用是常态，不该硬抽金额（重量/箱量/邮编会被误抓） |
| 内嵌图 | 按 `cid:` 引用判为正文图片，**不送 OCR** | 签名档、logo 不是单据；送 OCR 只会制造 `E_OCR_CASSETTE_MISS` 噪音 |
| 标题 | **只补** `bl_no` / `booking_no` / `container_no` | 箱号常只写在标题里；但标题里的到港时间不是单据日期，收件人也不是交易方 |


设计要点（完整论证见 `docs/DESIGN.md`）：

1. **LLM 是提案者，不是裁决者。** 金额错一位是财务事故，模型不能是最后一道门。
2. **规则轨与 LLM 轨字段级对账。** 不一致本身记成 `V-RECONFLICT`，把"模型内部犹豫"变成外部可观测信号。
3. **不虚构靠机器校验，不靠提示词。** LLM 每个字段必须返回原文证据，代码校验它是否真是原文子串，否则字段作废（`V-UNGROUNDED`）。
4. **OCR 混淆只标记不修复。** `4O.OO` 该是 40 还是 4 取决于原文，自动修复等于发明数据。
5. **更新语义三分：** 没提到 → 继承；原值为空 → 补全（不算变更）；两边有值且不同 → 变更。只有带证据的字段才允许覆盖。
6. **匹配多命中不猜。** 唯一命中才自动执行，多命中一律 `PENDING_REVIEW` 并把候选交给人。

---

## 4. Harness 工程（笔试验收第 8 条）

| # | 机制 | 落点 |
|---|---|---|
| H1 | 契约强类型：Pydantic `extra=forbid` + 输出 JSON Schema 校验 + `check` 子命令 | `schema.py` / `cli.py` |
| H2 | 提案-校验分离：LLM 只出 Candidate，决策只信 Issue 与 Match | `extract/` vs `validate/`、`matching/` |
| H3 | 证据锚定：字段级 evidence 必须命中原文子串 | `extract/llm_extractor.py::_ground` |
| H4 | 依赖可回放：OCR 走 cassette（按 `sha256+task` 键），LLM 走 `temperature=0` + 结果缓存 | `ocr/providers.py`、`extract/llm_extractor.py` |
| H5 | 置信度路由：金额 0.90 / 币种 0.90 / 名称 0.60，低于阈值转人审 | `decide/planner.py` |
| H6 | 失败不丢数据：任一层异常 → `Issue(blocker)` + 尽最大努力产出 | `pipeline.py` |

**评测不是"跑通就行"**：`scripts/evaluate.py` 出字段级 P/R/F1、操作类型混淆矩阵、风险召回、记录级完全正确率，`--gate` 在 F1<0.98 或风险召回<0.95 时非 0 退出，可直接挂 CI。当前样本集 11 个字段全 1.000、风险召回 1.000。

---

## 5. 我的思考：对需求的 10 点补充（验收第 9 条）

> 完整 14 条见 `docs/REFLECTIONS.md`，其中第 12～14 条是被真实数据和真实 OCR 打出来、推翻我原判断的三条。这里放最能说明「为什么这么做」的 10 条，每条都是「现象 → 判断 → 落地」，不写空泛议论。

### 5.1 需求本质不是「文档解析」，是「账务数据的可信变更」

错误代价是不对称的：少提一笔费用，财务对账时补上，成本一小时；多提一笔、金额错一位、AR/AP 方向反了，**账已经记进去了**，要冲销、要跟对方代理对账、可能触发客户争议，成本一天到一周。

所以定位不是「全自动入账」，而是**自动化的对账助理 + 高召回的人审分流器**：机器吃掉 80% 无歧义的单，剩下 20% 不是失败，而是它的第二个产品功能——把不确定性整理成人能 30 秒决策的形式。这条判断决定了整个架构：所有「不确定」必须有出口（`PENDING_REVIEW`），不能被「最可能的答案」填平。只按功能清单做，很容易写出「看起来很智能、每单都给答案、但错的地方你看不出来」的系统——笔试能拿分，上线当天被财务关掉。

### 5.2 更新的难点不是匹配，是区分「没提到」「改成空」「抽失败」

三种「空」长得一模一样：

| 情况 | 输入材料里 | 正确处置 |
|---|---|---|
| 材料没提这个字段 | 抽不到值 | 继承原值 |
| 明确写 `Payment Term: /` | 抽到空字符串 | 是「清空」还是「未填」？需人工或配置裁决 |
| 写了但抽不出来 | 抽不到 + 该区域 OCR 失败 | **不能继承，转人工** |

如果实现成「`new_value is not None` 才覆盖」，后两种会静默继承：第二种可能漏掉客户真正的清空意图，第三种更糟——**把抽取失败伪装成「未变更」，系统以为自己在保守，实际在丢信息**。解法是给每个字段挂 `Provenance`（抽取器 + 原文 span + 置信度），让「有没有证据」变成可判定的事：无 `prov` → 继承；有 `prov` 且值不同 → patch；有 `prov` 但置信度低 → 不 patch 转人工（`P-LOW_CONFIDENCE`）。**「能证明」和「抽到了」必须分开**。

### 5.3 AR/AP 不是单据属性，是「主体 × 方向」的向量

题目特意写「不能仅根据 Debit Note / Credit Note 直接判断」，这句话在暗示一个常见错误：把 DN 硬编码成 AR、CN 硬编码成 AP。真实业务里同一张 Debit Note，在起运港代理账上是 AP（对方收我钱），在目的港代理账上是 AR（我收对方钱）——**单据类型只决定符号，交易方向才决定主体**。

验证方式不是读代码，是换主体跑：同一份 `debit_note_DC-108478.pdf`，`PltPlt` → `null`、`AEBO` → AP、`NEPTUNE`（签发方自己）→ **AR + CREATE 自动入账**。另外题目没规定的 Credit Note 落账口径（mirror 冲销 / same_side 红字），我没有替客户选，做成 `credit_note_policy` 配置项——**需求没写的地方，暴露成配置比替客户决定更专业**。

### 5.4 OCR 混淆：检测该做，「自动修复」是陷阱

`4O.OO` 该修成 `40.00` 还是 `4.00`？取决于原文印刷体，而 OCR 已经把它糊掉了。擅自修复：修对了没人知道，修错了账单入账，且**没留下任何「我改过」的痕迹**——这是最坏的一种自动化。

分界线的判据是**候选集是否封闭**：币种只有 180 个合法值，`USO`→`USD` 是白名单**验证**，纠错有意义；金额是无限空间，任何修复都是发明。所以金额、单号、箱号命中混淆只标 `V-OCR_CONFUSION` 并强制人审，`validate/ocr_repair.py` 只返回 finding，**永不返回值**。

### 5.5 双轨的价值不是「更准」，是「不一致时暴露不确定性」

单轨系统再准，也不会告诉你「这条它自己也没底」。规则轨负责高确定低语义的字段（单号、日期、金额，正则命中的字符串是硬证据），LLM 轨负责高语义低确定的字段（签发方到底是哪一方、这笔 Destination Charges 归成哪个标准费用名、材料是否在表达「变更」意图），冲突本身落成 `V-RECONFLICT` 并升级。

**双轨的真正收益是把「模型内部的犹豫」变成系统外部的可观测信号**——这是 harness 工程里最便宜的一处收益，比调 prompt 有效得多。

### 5.6 幂等是邮件场景的生存条件，不是加分项

输入是 EML，就意味着天然会重复投递：同一封邮件被转发、被规则重复归档、被 IMAP 重复拉取、用户手动再拖一次。如果同一封邮件跑两次就产生两张账单，这个系统在真实环境里一天就会把账搞乱。

幂等键做成双层：文件级 `sha256`（同一文件重复处理直接命中缓存）+ 业务级 `(reference_no, charge_item, amount)`（代理重发的 PDF 和扫描件抽出同一笔费用，也要识别成「同一笔」而不是两条 `CREATE`）。后者正好与 G5 跨单重复检测共用一套判定。

### 5.7 「不虚构」不能靠提示词，必须靠机器校验

题目第七条反复强调不得虚构、抽不到用 null。这类要求写进 prompt 有用，但**不能作为唯一保证**——LLM 在长尾上不可预测，而「编造一个看起来合理的提单号」恰恰是它最擅长的失败模式。

落成两条硬规则：①**证据锚定**——LLM 每个字段必须返回原文片段，代码校验它是否真是输入文本的子串，不匹配则字段作废（`V-UNGROUNDED`）；②**Schema 封闭**——`extra="forbid"` + 固定键集合，模型想加字段、改键名、把金额写成 `"40.00 USD"`，全部在解析层被拒。**提示词是软约束，schema 是硬约束，生产系统只信硬的。**

### 5.8 「更先进的技术」不等于「更好的结果」

我原本假设 OCR 比 PDF 文本层强（OCR+大模型是「现代做法」），所以默认让 OCR 覆盖文本层。真实网关跑同一份 Debit Note 的结果推翻了这个假设：

| 字段 | 文本层 | 网关 OCR |
|---|---|---|
| 单号 | `DC-108478` | **空** |
| 币种 | USD | **空**（`chargeList` 压根没有币种字段） |
| 明细 | 65 + 1310.40 + 35（3 条） | 100 + 1310.40（2 条，65 和 35 被合并） |
| 合计 | 1410.40 | 1410.40 |

总额都对，但**「总额对」不等于「能入账」**：丢单号会让 T1 匹配直接失效，还会被判成 `CREATE` 重复入账——而这一切发生在「返回 200、`success: true`」的表象之下。所以改成「文本层为主源、OCR 只补标识字段」，两条路都跑比选一条好。反过来说，传真件、拍照件根本没有文本层，OCR 就是唯一选择（那张 ZIM 扫描件 26.90 正是这样跑通的）。**不是「OCR 不好」，是「有文本层时不该让 OCR 覆盖它」。**

### 5.9 真实数据的作用是「证伪」，自建金标只能自证

合成样本上 10 条记录全对、当时 122 项测试全绿、评测 F1 = 1.000——**因为样本是我按自己的正则生成的，它证明的只是「正则和样本自洽」**。换成题目网盘的真实附件，第一轮立刻 33 条记录全部 `PENDING_REVIEW`、明细合计 4.07 亿 vs 单据总计 1410.40，提单号 `260801188`、银行路由号 `121000358`、邮编 `7032` 都被当成金额。

由此三条更硬的经验：①**「抽得全」和「抽得准」在真实数据上是矛盾的**，解法不是加正则，而是先定位语义区块（费用表头 → 合计行），把搜索空间从「整页」缩到「几行」——这是结构约束，不是模式匹配；②**评测集必须来自外部**，自建金标跑分再高也只是自证，指标从 1.000 掉到 0 才是真实起点；③**全拦不等于安全，等于没有系统**，合格边界是「该抽的抽对、该拦的拦住」。

### 5.10 别用「自动入账了几条」当质量指标

上一版真实数据判出了 3 条 AP，看着比 0 条有说服力，于是我没有质疑依据。回头查发现那 3 条是错的：`recipient` 抽到了页脚 `NEPTUNE SHIPPING LIMITED (LAX) Powered by / 湃乐多（上海）航运科技有限公司` 里的 SaaS 水印，把「单据系统供应商」当成了「付款人」。因为挂了 `F-PARTY_INFERRED` blocker，它被拦在 `PENDING_REVIEW` 没真入账，但**字段本身是错的**——换个不检查 issue 的下游就是一笔错账。

更危险的是第二个错误：**用产出量当质量指标，会诱导系统多猜**——猜对了算准确率，猜错了靠兜底拦，跑分很好看，实质是「靠兜底假装准确」。好指标是「判出来的那些对不对 + 判不出的理由清不清楚」。还有一条通用的：**同一字段，位置比内容更重要**——`湃乐多` 出现在 `Powered by` 后面和出现在 `BILL TO` 后面是两个完全相反的意思，只判断「字符串在不在文中」的抽取层一定会踩这个坑。

---

## 6. 仓库结构

```text
docs/
  REQUIREMENTS_ANALYSIS.md   需求条目化 + 歧义取舍 + 验收映射
  DESIGN.md                  技术方案、AR/AP 规则表、匹配算法、假设清单
  DEV_SPEC.md                施工规格：模块签名、配置、错误码、测试计划
  REFLECTIONS.md             验收第 9 条：14 条思考与业务补充（README §5 是其中 10 条的摘要）
  REAL_DATA_NOTES.md         真实网盘数据的落地记录：7 处版式差异与修法、结果对照
src/shipping_bill_agent/
  config.py schema.py pipeline.py cli.py audit.py utils.py errors.py
  ingest/   dispatch eml_parser excel_parser word_parser pdf_parser
  ocr/      client providers normalize
  extract/  rule_extractor rule_dates reconcile llm_extractor prompts
  normalize/ charge_map currency amounts
  validate/ validators ocr_repair
  matching/ store matcher
  decide/   ar_ap planner
data/
  real/               题目网盘原始样本（Debit Note / Arrival Notice / PRE-ALERT 邮件 / 扫描图）
  samples/            合成样本（pdf/xlsx/docx/png/eml/xls）
  ocr_cassettes/      OCR 回放磁带（按文件 sha256 命名）
  golden/expected.json 金标
  existing_bills.json 历史账单（匹配/更新用）
  charge_aliases.yaml 费用别名表（可热改，外置维护）
scripts/  make_samples.py evaluate.py
tests/    216 项，覆盖每个闸门、每级匹配，以及真实样本回归（test_real_samples.py）
```

---

## 6.1 AR/AP 为什么不判？（真实数据全 null 的解释）

先看三套数据的分布：

| 数据集 | 记录 | operation | AR / AP / null |
|---|---|---|---|
| 合成样本 `data/samples` | 10 | CREATE 4 / UPDATE 1 / NO_CHANGE 4 / PENDING 1 | **AR 2 / AP 8 / 0** |
| 旧格式样本 `data/samples/legacy` | 4 | CREATE 4 | **AR 4 / AP 0 / 0** |
| 真实网盘数据 `data/real` | 7 | PENDING_REVIEW 7 | AR 0 / AP 0 / **null 7** |

**判向规则**（`decide/ar_ap.py`，对应题目四.6「结合签发方、接收方、记账主体和收费方向」）：

| 记账主体 | 单据类型 | 结果 |
|---|---|---|
| = 签发方 | DN / Invoice / Arrival Notice | **AR**（对外收费） |
| = 接收方 | DN / Invoice / Arrival Notice | **AP**（被收费） |
| = 签发方 | Credit Note（mirror） | AP（冲销） |
| = 接收方 | Credit Note（mirror） | AR（冲销） |
| 两者都不是 | 任意 | `null` + `F-ARAP_UNKNOWN` |

**同一份单据换主体，判向立刻变化**（`debit_note_DC-108478.pdf`，issuer = NEPTUNE）：

| 记账主体 | posting | operation |
|---|---|---|
| PltPlt | null | PENDING_REVIEW |
| AEBO TCO IMPORT LLC | AP | PENDING_REVIEW（`F-PARTY_INFERRED`） |
| **NEPTUNE SHIPPING LIMITED** | **AR** | **CREATE（自动入账）** |

同一份文件、同一套代码，主体换成签发方自己就判出 `AR + CREATE`。

**真实批次为什么全是 null**：Debit Note 页脚是

```
NEPTUNE SHIPPING LIMITED (LAX) Powered by
湃乐多（上海）航运科技有限公司
```

这是「Powered by 湃乐多」的 **SaaS 水印**（说明 NEPTUNE 用了湃乐多的单据系统），
不是「单据开给湃乐多」。早先版本把它当接收方，判出了 3 条 AP —— 那是**错的应付**。
现在水印被识别并排除，`recipient` 归 null，AR/AP 不判。

剩下 4 条同理：Arrival Notice 缺接收方、EML 无单据类型、扫描件无交易方字段。
硬判就是违反题目七.1「不得虚构输入材料中不存在的数据」。

> 一句话：**该自动的自动，不该猜的不猜。** 全 null 不是「全部转人工」，是「这 4 份文件本来就不是我方的账」。

## 7. 能力边界（明确写死，避免误读）

| 项 | 状态 |
|---|---|
| 题目附件数据（百度网盘） | **已获取并跑通**（`data/real/`，7 条记录见 `docs/REAL_DATA_NOTES.md`）。真实数据里没有 Excel/Word 样本，这两条能力仍只由合成样本覆盖 |
| OCR 网关 `gateway.pltplt.com` | **已真机跑通并落地真实结果**（2026-11-01）：扫描件传公网图床 → 网关返回 `HTTP 200`，抽出 2 条费用（5.00 + 21.90 = 26.90），响应存为 cassette 可离线复现。⚠️ 关键约束：`file` 必须是**网关自己下载得到**的地址——本地路径、`file://`、需登录的网盘链接都不行；给无效地址时服务端会卡在下载上直到超时（这正是我早前误判「网关不可达」的原因）。PDF 的真实 OCR **也已跑通**（2026-11-01，四份 PDF 全部调过网关）。但实测发现 OCR 对这份账单**比文本层差**：单号空、币种空、65 和 35 被合并成 100。所以策略是**文本层为主源、OCR 只补标识字段**（`bl_no`/`booking_no`/`container_no`），详见 `DEV_SPEC §8.6` |
| `.doc` / `.xls` 旧二进制格式 | **已支持**。`.xls` 用 `xlrd` 读（多 sheet、表头推断）；`.doc` 用 `textutil`（macOS 自带）→ `soffice` → `antiword` 分层转换，并用制表符还原表格。两者都没有时报 `E_DOC_NO_CONVERTER` 并说明装什么 |
| PDF 表格结构 | 文本层 PDF 不做表格识别，靠"固定宽度列切分"；扫描件靠 OCR |
| OCR 返回结构 | 网关返回的是**已结构化的单据**（`data.list[].chargeList/ctrList/shipper/consignee…`），不是 OCR 文本。`chargeList` 的真实键是 `chargeItem / amount / chargeCtr`——**不含币种**，所以扫描件的 `currency` 只能是 `null`。归一层按这个结构摊平，键名未知的数组交给通用 `list[dict] → 表格` 处理 |
| 跨币种勾稽 | **支持多币种，但不做汇率折算**：按币种分组校验；单据逐币种给总额时逐币种比对，只给一个总额时降级为 `V-MULTI_CURRENCY` info 并列出各币种合计，绝不跨币种相加 |
| 人审 UI / 审批流 | 本期只输出 `report.json` 供下游人审消费 |

---

## 8. 常见问题

**Q：`pytest` 报 `FileExistsError: mkdir '/private/var/.../pytest-of-xxx'`？**
某些受管控环境会拦截系统临时目录创建。`pyproject.toml` 里已把 pytest 的 basetemp 固定到项目内（`--basetemp=.pytest_tmp`），默认 `pytest` 即可跑；如仍异常，显式传 `pytest --basetemp=./tmp`。

**Q：想让 PDF 一律走 OCR？** 默认就是 `ocr`（`--pdf-strategy ocr` / `SBA_PDF_STRATEGY=ocr`），PDF 和图片都送网关。网关失败时自动回退文本层，trace 记「结果来自文本层，非 OCR」，不写 `errors.json`。想省 OCR 预算才用 `auto`。

**Q：网关连不上，整批都跑不了吗？** 不会。`pdf_strategy=ocr` 下 PDF 会先试 OCR，失败回退它自己的文本层（PDF 解析阶段已经拿到）；只有**扫描件**（无文本层）才真失败并进 `errors.json`。所以断网时 PDF 仍能出结果，只是结果标注为「文本层来源」。

**Q：为什么提单附件抽出来是 0 条？** 因为提单本来就没有费用明细。系统识别出 `BILL OF LADING/WAYBILL` 后不抽费用（否则 `10715.000KGS` 重量、`PAGE: 1 OF 1` 页码、邮编都会被当金额），并在 trace 里记一条 note 说明「非计费单据，未抽取费用（符合预期）」——不报错、也不静默消失。

**Q：PDF 走了 OCR，为什么结果里还有单号、币种？** 因为策略是「文本层为主源、OCR 只补标识」。
实测同一份 Debit Note：文本层给到 `DC-108478` + USD + 3 条明细（65 / 1310.40 / 35）；网关 OCR 给的是
单号空、币种空、明细 2 条（65+35 被合并成 100）。让 OCR 覆盖文本层会直接丢单号——那是 T1 匹配的必需键。
所以网关照样调（满足题目「PDF 通过 OCR 识别」），结果放副源位，只用它补文本层没有的
`bl_no` / `booking_no` / `container_no`，provenance 标 `ocr:supplement`。trace 里能看到每份 PDF 的处理方式。

**Q：`check` 为什么要加 `--allow-extensions`？** 不需要了。`run` 默认带 `charge_item_raw` 扩展字段，`check` 默认也放行；要严格按题目契约（无扩展字段）校验时用 `check --no-allow-extensions`，或 `run --strict-schema` 后再 check。

**Q：同一张单里出现两行相同费用，会被去重掉吗？** 不会。业务上两个箱子各收一次 THC 是正常的，所以两行都保留，只在第二条上标 `V-DUP_LINE`(warn) 提示人工确认是否重复录入。

**Q：单据没写接收方，系统却判了 AP？** 现在不会了。主体名只出现在备注/付款说明句、或 `Powered by <我方>` 这类 SaaS 水印里，都不算接收方声明；即使被推断出来，也会加 `F-PARTY_INFERRED` blocker 转 `PENDING_REVIEW`，不自动定 AR/AP。

**Q：换记账主体会怎样？** `--entity "Destination Agent"` 后同一批单据的 AR/AP 会整体翻转——这正是"AR/AP 不是单据属性，而是主体视角下的方向向量"的验证方式。
