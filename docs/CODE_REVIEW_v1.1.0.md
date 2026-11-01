# Code Review 报告 · ShippingBillAgent v1.1.0（全量初版）

| 项 | 值 |
|---|---|
| 评审对象 | 无 git 仓库 → 全量代码（`src/` `scripts/` `tests/` + 配置 + 别名表），64 文件 / +7928 行 |
| 档位 | **完整档**（>500 行、涉资金判向、跨 8 个模块） |
| 风险等级 | **高**（计费/资金类，属「人必须亲看」类型；错误输出会直接进财务系统） |
| 评审者 | L1 `agent-695de2a0` · L2 `agent-99305387` · L3 `agent-d4ee34d7`（均 Explore / fresh 上下文 / 只读） |
| 工作区完整性 | 评审前后 `shasum -a 256` 全量比对 → **一致**（65 个文件），评审者未改动任何文件 |
| 输入边界 | 只给「需求卡 + 全量 diff」，未给写码对话、测试输出、截图 |
| 轮次 | 第 1 轮 |
| 评审时间 | 2026-11-01 09:40 |

**门禁结论：修完可合入**

7 条重要问题全部在现有结构内可修，无设计层推翻；但**其中 3 条会直接产出错误金额或错误 AR/AP**，修完前不能作为交付件。

---

## 一、重要问题（阻塞交付）

| # | 位置 | 层 | 证据（均已本机复现） | 建议 |
|---|---|---|---|---|
| A1 | `normalize/amounts.py:87` | L2 | `_NUM_RE = \d+(?:[.,]\d+)*` 不容空格，而 `_THOUSANDS_RE` 把空格当千分位，两条规则打架。实测 `parse_amount("1 234.56") → (1.0, 0.9)`、`"12 345.67" → 12.0`、`"2 400,00" → 2.0`。置信度仍为 0.9，**超过 `confidence_amount` 阈值**，CREATE 路径无任何闸门 → 金额被少报 1000 倍后直接入账，无 issue | 先按 `\d{1,3}(?:[ ,.]\d{3})+(?:[.,]\d+)?` 整体匹配再判定分隔符语义；多段数字判不出时返回 `(None, 0.0)`，宁可转人工也不取第一段 |
| A2 | `validate/validators.py:188` + `pipeline.py:188` | L2/L3 | 同单去重按 `(charge_item, amount, currency)` 丢弃后一条，被丢弃行**连同它唯一的 `V-DUP_LINE` 一起消失**：不进 result、不进 report（`_report_dict` 只遍历 kept）、不进 errors、recorder 无计数。实测一张单两行 `THC 40.00 USD`（两箱各收一次）→ 4 个产物里只有 1 条记录，**40.00 凭空消失且零痕迹**。需求七/八.7 要求「识别重复」，实现却做成了「静默删除」 | 被丢弃行以 `PENDING_REVIEW` + `V-DUP_LINE` 保留进输出，并把丢弃摘要写进 report 单据级区块与 recorder 计数；去重键补 quantity/行号 |
| A3 | `pipeline.py:263` | L2/L3 | `entry.provenance["currency"]` **无条件写入**（无币种时 confidence=0.0），而 `planner.py:71` 把 `prov is None` 当作「无证据→继承」的唯一判据。实测历史账单 `currency=USD`、输入表格无币种列 → `changed={"currency": None}`，输出 `currency: null`，审计链还写「输入材料明确要求变更 币种 → None」 | 判据从 `prov is None` 改为 `has_evidence`（span 或值非空）；`currency`/`charge_item`/`amount` 三处无条件写 provenance 的地方统一补证据判断 |
| A4 | `extract/rule_extractor.py:822-849` | L1 | `_find_recipient` 标签匹配失败后，只要主体名出现在**任意一行**（备注、页脚、通知方）就返回 `recipient=主体`（conf 0.6），`planner` 对 `posting_type` 无置信度闸门。实测单据写 `Remarks: destination charges payable by PLTPLT PTE LTD` → 输出受方 `PLTPLT`、`posting_type=AP`、reasoning 断言「记账主体是本单接收方」。**真实数据 `out/real/` 的 3 条 AP 全部由该推断得出（`rule:entity_in_doc`, 0.6）**，不是单据明写。违反七.1「不得虚构」 | 推断值与明写值必须可区分：`extractor` 带 `:inferred` 后缀，并在 planner 增加「接收方为推断值 → 不得自动定 AR/AP，转 PENDING_REVIEW」。备注句应排除（`remark/note/payable by` 上下文不参与判向） |
| A5 | `matching/store.py:17,150` | L2 | ① `normalize_key` 不折叠分隔符：历史 `DN-202609-001` vs 本次 `DN/202609/001` → `tier=None`；② 匹配只比 `charge_item` 不比 `charge_item_raw`：历史存 `THC`、本次归一为 `Terminal Handling Charge` → `tier=None`。两者都实测复现（`tier=None, bill_id=None`），后果是**同一张单被判 CREATE 重复入账**，且 `has_duplicate` 同样匹配不上、不会兜底告警 | 匹配键对 `[/\\\-_.\s]` 统一折叠；查询时对历史记录同时尝试 `charge_item` 与 `charge_item_raw`（都过 `normalize_charge` 再比） |
| A6 | `decide/planner.py:17-26,57-81` | L2 | `FIELD_ORDER` 只含 8 个字段，`issuer/recipient/document_type/reference_no/bl_no/container_no/booking_no` 既不参与 diff 也不继承，永远输出新单据抽到的值。实测历史 `issuer="K Line Logistics Ltd"`、输入写 `Issued by: Destination Agent` → 输出 `operation=NO_CHANGE` 但 `issuer` 已被改成 `Destination Agent`，零 issue。直接违反七.6「不得覆盖未明确要求修改的字段」 | 单据级字段纳入继承白名单（无证据或与历史不一致 → 取历史值并记 info；交易主体变更另记一条「需人工确认」） |
| A7 | `ingest/excel_parser.py:36`、`ingest/word_parser.py:16` | L1 | 需求三明确列出输入含「**XLS**/XLSX」「**DOC**/DOCX」，代码对旧格式直接 `raise UnsupportedFormatError`，仅 xlsx/docx 可用；`data/real/` 里也恰有一份 `legacy_rates.xls` 进 `errors.json`。属需求范围内格式未实现，不是错误处理策略问题 | 二选一：① 接 `xlrd==2.0.1`（仅 xls）与 `antiword/textract`（doc）；② 在 README 与交付说明里显式列为「已知限制 + 影响范围」，不要只在代码里抛错 |

## 二、小问题（不阻塞）

| # | 位置 | 层 | 证据 | 建议 |
|---|---|---|---|---|
| S1 | `decide/planner.py:77` | L2 | 原值 `null` → 新值有值归为 `filled`，**不参与 operation 判定**：实测历史 `amount=null`、本次 40.00 → `operation=NO_CHANGE` 但 payload 带 40.0。下游按 NO_CHANGE「不动」处理则永远补不上，按 payload 落库又与 operation 语义矛盾 | 「补全」单独成一个 operation 语义（或 `UPDATE` + `fill_only` 标记），至少不能报 NO_CHANGE |
| S2 | `normalize/currency.py:95` | L2 | `_UPPER_CODE_RE` 把任意三位大写词当币种：实测 `normalize_currency("ALL CHARGES PREPAID") → ("ALL", 0.85)`、`"XXX"→XXX`、`"XTS"→XTS`，且 `is_iso4217("XXX")` 为 True 故校验不拦 | 排除 `XXX/XTS/XAU` 等非货币码；含全大写英文句子的上下文不参与三位码提取 |
| S3 | `normalize/charge_map.py:122` | L2 | `contains` 分支取最长别名：实测 `"THC + DOC FEE"`、`"DOC FEE / THC"` 都归为 `Documentation Fee(0.9)`，另一项丢失且置信度不降，不触发 `N-UNMAPPED_CHARGE` | 命中 ≥2 个不同 canonical 时返回 `unmapped` + 记 `N-MULTI_CHARGE`，或先按 `+ / ,` 切分再逐项归一 |
| S4 | `extract/rule_extractor.py:74` | L2 | `TOTAL_PATTERNS` 允许 MONEY 的「3 位大写字母=币种前缀」：实测 `_find_money_line(["TOTAL QTY 4"], TOTAL_PATTERNS) → 4.0`、`"TOTAL PCS 10" → 10.0`。EML 正文若先出现 `TOTAL QTY 4`，总额取 4，产出「明细 40 与总计 4 不一致」的假 blocker | 总额行的金额前缀须是白名单币种或货币符号；`QTY/PCS/CTNS/KGS` 走黑名单；优先取最后一个 TOTAL 行 |
| S5 | `pipeline.py:406` | L3 | 跨文件判重键用 `reference_no or ""`：实测两份无关 xlsx（均无单号）各含 `Handling Fee 40 USD` → 第二份被判 `V-DUP_BILL` blocker | 无单号时不做跨文件判重（退化为文件 sha256 相同才判） |
| S6 | `extract/rule_extractor.py:947` | L1 | `document_date` 缺失时用邮件 `Date` 头填充（实测邮件 2026-09-21 vs 单据 2026-09-20），输出层看不出是推断值，CREATE 路径无闸门 | 推断日期标 `inferred` 并在 reasoning 明示「取自邮件发信时间」 |
| S7 | `ingest/dispatch.py:126` | L3 | `expand_inputs` 用 `elif path.is_file()`：实测 `run(['/tmp/does_not_exist.xlsx'])` → `result=[]`、`errors=[]`、**退出码 0**，与「这批本来没费用」不可区分 | 显式传入但不存在 → 写 `E_INPUT_NOT_FOUND` 并让 CLI 非 0 退出 |
| S8 | `pipeline.py:379` | L3 | `if not extraction.charges: return []` 静默丢弃：实测 PNG（OCR 失败）→ `result=[]`、`errors.json` 只有 OCR 错、`report.json` 里**没有该文件的 file 记录**；空 xlsx 同样零痕迹 | 该路径补 `E_NO_CHARGE`（带路径）或 report 里显式标 `no_charge_found` |
| S9 | `pipeline.py:389` | L3 | `degraded = not llm.enabled`：开了 Key 但每次调用都异常时只 `bump("llm_errors")`，`report.json` 仍 `degraded: false`，实际是纯规则轨 | 拆成两个布尔：`llm_enabled` / `llm_failed`；失败写 errors |
| S10 | `config.py:98,105,106,64` | L3 | `per_file_timeout_s`、`ocr_concurrency`、`pdf_text_layer_min_chars`、`ocr.cache_enabled` 只读环境变量、全仓无引用（grep 确认）：调 `SBA_FILE_TIMEOUT` 无效；网关 300s×4 次重试 → 单文件最坏 ~1207s，一批 10 个 PDF 可挂 3 小时且无兜底 | 实现（线程/信号 + join timeout）或删除，别留「看起来生效」的配置 |
| S11 | `validate/ocr_repair.py:98` | L3 | `repair_currency` 只被 `tests/test_validate.py:108` 调用，`normalize_currency` 从不调它 → 币种 OCR 混淆在生产路径既不修也不报 | 接进 `normalize_currency` 失败分支并记 issue，否则删函数+删测试 |
| S12 | `extract/reconcile.py:40` | L3 | `for f in set(rule.scalars) | set(llm.scalars)` 迭代顺序依赖字符串 hash 随机化 → `V-RECONFLICT` 追加顺序、进而 `ai_reasoning` 数组顺序在不同进程不同（LLM 开启时 result.json 不保证字节一致） | 按固定顺序（`schema.SCALAR_FIELDS`）迭代 |
| S13 | `pipeline.py:509-530` | L3 | 四个产物 `write_text` 直接覆盖、无临时文件+rename；且 507 行的排序只作用于 result，report.records 保持原序 → 两份文件同一条记录下标不对应；中途失败留下「result 有、report/errors/trace 缺」的半套产物 | 写同目录 `.tmp` 后统一 rename；report 复用同一份排序后列表 |
| S14 | `cli.py:62` vs `cli.py:105` | L3 | `run` 默认 `extension_fields=True`，`check` 默认 `--allow-extensions=False` → **同一仓库两个子命令对默认契约判断相反**，run 完直接 check 必失败（`test_pipeline_e2e.py:59` 是手工传参绕过的） | 统一默认值，或 check 读 result 旁的元数据判断 |
| S15 | `validate/validators.py:147,233` | L3 | ① 勾稽把明细直接相加与单一 total 比，隐含「一单一币种」，但 `ChargeCandidate.currency` 是逐行的、`_majority_currency` 也承认多币种；② `_document_issues` 在 `_duplicate_line_indices` 之前算，**把即将被丢弃的重复行算进 detail_sum**，于是「100+95+95 vs 总计 195」的正常单会被报成「明细 290 与总计 195 不一致」，归因方向错 | 先算 drops 再用 kept 子集勾稽；按 currency 分组后再比，多币种降级为 info 并注明未换汇 |
| S16 | `pipeline.py:337-352` | L3 | EML 只要附件抽出 ≥1 条费用，正文费用就被整体丢弃且不留 note（实测真实邮件正文 `H/C $50` 被丢弃）。代码隐含「附件有费用则正文费用必重复」，仓库无比对逻辑支撑 | 正文与附件都抽完后再按 `(ref, charge, amount)` 去重，保留差异项并记 note |
| S17 | `extract/prompts.py:26` vs `:7` | L3 | 提示词要求「只能从 `<<<DOC 开始>>>` 之间抽取」，但 `{tables_block}` 与 `{hints}` 放在分隔符**之外**；而 `_ground` 只查 `doc.text`（OCR 表格块不进 doc.text）→ 「模型引用表格 → 锚定失败 → `continue` 静默丢字段」闭环，且无计数 | 表格与线索移进 DOC 分隔符内，或显式声明为受信证据域；锚定失败计入 `llm_errors` 并记 note |
| S18 | `tests/test_planner.py:112` | L3 | `any(code == "P-LOW-CONFIDENCE" or code == "P-LOW_CONFIDENCE")` —— 代码只会产出前者，属「断言迁就实现」的写法 | 收敛成单一名 |
| S19 | `extract/rule_extractor.py:947`（本次复核新发现） | — | 真实数据的 EML 记录 `issuer = "\"luka@pltplt.com\" <luka@pltplt.com` —— 发件人原始头直出，带引号的显示名与地址混一起 | 清洗为显示名或置 `null`，不要把 `From` 原文塞进 `transaction_party.issuer` |
| S20 | `extract/rule_extractor.py`（本次复核新发现） | — | xlsx 展平后签发方带分隔符残留：实测输出 `issuer = "| ABC Lines Pte Ltd"` | 取值后 `strip(" |")`，或在 excel 展平处不写分隔符到单元格内 |
| S21 | `docs/REQUIREMENTS_ANALYSIS.md:106-107` | — | 歧义表里同一行「document_type 是否只有 DN/CN/Invoice」重复出现两次 | 删一行 |

## 三、已有问题（非本次引入，必须记录）

| # | 位置 | 说明 | 处置 |
|---|---|---|---|
| 1 | `ocr/client.py` | 当时判断网关不可达（curl/Python 均超时）；cassette 响应形状是自造的，题目未给响应结构 | **2026-11-01 更正**：网关可达，超时是因 `file` 地址无效；真实响应是结构化单据（非 OCR 文本），归一层已按真实结构改写（DEV_SPEC §8.5） |
| 2 | `data/real/` | 真实样本无 Excel/Word，验收 3 的这两条能力只有合成样本覆盖 | 已在 `docs/REAL_DATA_NOTES.md` 标注 |

## 四、已忽略（误报，由输入包范围导致）

> 这 3 条是我的输入包设计问题造成的，不是代码问题。原始报告里它们被标成「重要」，此处按事实降级并记录理由。

| # | 原始断言 | 事实 | 处置 |
|---|---|---|---|
| 1 | 「64 个交付文件里不存在任何 `.md`，验收 8/9 无交付物」 | 输入包只含 `src/scripts/tests`，未含 `docs/`；交付 zip 内实际有 **6 个 .md**（README + 5 篇 docs） | 白名单：`packet-scope-docs-excluded` |
| 2 | 「`data/samples` / `data/ocr_cassettes` 未交付，全新检出后测试必然失败」 | 交付 zip 内实际有 **12 项** `data/samples` 文件与 cassette | 同上 |
| 3 | 「`pyproject.toml` 引用不存在的 `README.md`」 | `README.md` 存在（1 万余字） | 同上 |

## 五、待确认

| # | 位置 | 评审者/复核者理由 | 人判定 |
|---|---|---|---|
| 1 | A7 `.xls/.doc` | 题目写「XLS/XLSX」「DOC/DOCX」，但验收 3 只说「解析 Excel、Word 中的费用表格」。若考官只测 xlsx/docx，则「显式拒绝 + 文档说明」可接受 | 待定 |
| 2 | A2 同单两行相同费用 | 业务上「两个箱子各收一次 THC 40」是合法场景，还是必属重复录入？取决于计费口径 | 待定（倾向合法 → 必须保留并标疑） |
| 3 | S15 单币种假设 | 真实数据 5 条全 USD，无法证伪；若业务上会出现混合币种单据则升级为重要 | 待定 |

## 六、cross check 提示（机器审不出来的部分）

- [ ] **我上一轮的口径要更正**：对外说过「真实数据 3 条 AP」，这 3 条全靠 `rule:entity_in_doc`（0.6）推断——单据没写接收方。A4 修完后，这 3 条应降为 `PENDING_REVIEW`，即真实数据的「正确结果」会从 3 条 AP 变成 0 条自动入账。这不是变差，是从「猜对了」改成「不猜」。
- [ ] 计费口径：`THC` 一单两行是否合法（决定 A2 的修法方向）。
- [ ] 财务侧是否按 `operation` 语义执行（决定 S1「补全」的严重级：若下游只认 UPDATE，S1 会让补全永久丢失）。
- [ ] 回滚：本版无状态、无迁移，回滚 = 换回旧 zip，无数据风险。

## 七、刹车检查

- [x] 本轮为第 1 轮
- [x] 同类问题一次指全（限额/币种/名称/匹配键四类各只列一条，未分批）
- [x] 未重复提
- [x] 未达第 4 轮阈值

## 八、签字

| 项 | 值 |
|---|---|
| 对应 commit | 无 git（`ShippingBillAgent-1.1.0.zip`，65 文件校验和快照见 `/tmp/before.sha`） |
| 签字人 | **待用户签** |
| 签字含义 | 核对过改动的意图、假设、设计，同意上线 |

> agent 的「同意」不算 approval。本报告是评审意见，不是签字。

---

## 附 · 修复建议顺序（同一轮内一次改完再复审）

1. **A1 金额解析**（会直接产生 1000 倍错误，且现有测试没覆盖空格千分位）
2. **A4 接收方推断**（决定 AR/AP，且真实数据 100% 依赖它）+ A6 字段继承（同类「覆盖不该覆盖的」）
3. **A2 重复行可见性**、**A5 匹配键归一**
4. **A3 空币种 provenance**（与 A6 同属继承判据，一起改）
5. **A7**：加 `xlrd` / 或在 README 写「已知限制」
6. 小问题按 S1→S17 顺序，S2/S3/S4 属同一类「过度匹配」，可一并处理

每条修完需补对应回归用例；建议新增 `tests/test_boundaries.py` 专放本次暴露的边界（空格千分位、两行相同费用、`DN/` vs `DN-`、无币种列、备注含主体名）。

---

# 九、处置结果（v1.2.0 整改）

> 按 skill 的「每一条都必须有结论」要求：以下逐条给出**已修 / 部分修 / 不修+理由**。
> 整改后：`pytest` 160 passed（原 130），`scripts/evaluate.py --gate` 通过，合成样本 10 条与整改前逐字段一致。

## 9.1 重要问题（7/7 已修）

| # | 处置 | 落点 | 验证方式 |
|---|---|---|---|
| A1 | **已修** | `normalize/amounts.py`：数字 run 支持「1 234.56」，空格分组非 3 位时不再取首段 | `test_boundaries.py::test_a1_*`（7 个参数 + 端到端 xlsx） |
| A2 | **已修（按确认口径：合法 → 保留并标疑）** | `validate/validators.py`：不再返回 drops；重复行保留、标 `V-DUP_LINE`(warn)，四条产物均可见；`pipeline` 不再过滤 | `test_g5_duplicate_line_kept_and_flagged`、`test_a2_duplicate_lines_are_kept_and_flagged`（断言 2 条记录都在） |
| A3 | **已修** | `pipeline._build_entry`：只有拿到币种才写 provenance | `test_a3_missing_currency_does_not_null_out_existing_value`（继承 USD） |
| A4 | **已修** | `_find_recipient`：备注/付款说明句排除，推断值 extractor 改为 `infer:entity_in_doc`；`planner` 新增 blocker `F-PARTY_INFERRED`，推断交易方不得自动定 AR/AP | `test_a4_remark_line_*` / `test_a4_inferred_recipient_never_auto_posts`；真实数据 3 条 AP 已降为 PENDING_REVIEW |
| A5 | **已修** | `store.normalize_key` 抹掉 `- / _ . 空白`；`query`/`has_duplicate` 用 `charge_keys()`（标准名/原始名/再归一） | `test_a5_reference_separators_are_folded` + 复现实验（`DC/800001` 与历史 `DC-800001` 现命中 T1） |
| A6 | **已修** | `planner.DOC_FIELDS` + `_diff_doc_fields`：单据级字段不参与 patch，不一致时保留原值并记 `P-DOC_FIELD_DIFF`；且在 AR/AP 判定**之前**执行 | `test_a6_issuer_is_not_overwritten_on_matched_bill` |
| A7 | **已修（按确认口径：所有说明格式都要支持）** | `.xls` 走 `xlrd`（多 sheet、表头推断、日期/布尔单元格）；`.doc` 走 `textutil → soffice → antiword` 分层转换并用制表符还原表格；无可用转换器时报 `E_DOC_NO_CONVERTER` 并说明怎么装 | `test_legacy_formats.py`（xls/doc 解析 + 端到端 4 条记录） |

## 9.2 小问题（21 条：19 已修、2 部分修）

| # | 处置 | 落点 / 说明 |
|---|---|---|
| S1 | 已修 | 新增 `P-FILL_ONLY`：原值为空→有新值记为 UPDATE；仅 `amount/currency/payment_term/charge_item/document_date` 触发，数量/单价/税额仍只写审计（避免把明细补全当成账务变更） |
| S2 | 已修 | 三位码必须与金额相邻或带 `CURRENCY` 标签；`XXX/XTS/XAU` 与非货币码拒收；`ALL/TRY/TOP/PEN` 等歧义词单独出现不认 |
| S3 | 已修 | `contains` 分支改词级匹配，命中多个标准名时返回 `ambiguous` + `N-MULTI_CHARGE` warn，保留原文 |
| S4 | 已修 | 新增 `_find_total`：`TOTAL QTY/PCS/CTNS/WEIGHT` 等行排除；强模式（GRAND TOTAL / TOTAL AMOUNT）优先 |
| S5 | 已修 | 无单号不参与跨文件判重；被判重复时补一条 ai_reasoning |
| S6 | 已修 | 邮件头日期 extractor 改 `rule:email_date`，审计里明确「日期取自邮件发信时间，入账前请核对」 |
| S7 | 已修 | 显式传入的不存在路径 → `E_INPUT_NOT_FOUND` 落 errors.json |
| S8 | 已修 | 解析成功但零费用 → `E_NO_CHARGE`；放在错误收集**之后**，不会与 OCR 失败重复报 |
| S9 | 部分修 | report.json 新增 `llm_enabled` / `llm_errors` 两个字段，能区分「未启用」与「启用但失败」；`entry.degraded` 语义未改（改动会牵动契约字段），留作后续 |
| S10 | 已修 | 删除 `per_file_timeout_s`/`ocr_concurrency`/`ocr.cache_enabled` 三个死配置；`pdf_text_layer_min_chars` 真正接进 `_needs_ocr`（配合 pdf_parser 新增 `text_chars`） |
| S11 | 已修 | `repair_currency` 接入 `normalize_currency`（仅限含数字的三位码，白名单唯一解才修，置信度 0.5）；同时补上数字→字母方向（`US0 → USD`） |
| S12 | 已修 | `reconcile` 按 `sorted()` 迭代，同一输入不再因 hash 随机化产生不同的 ai_reasoning 顺序 |
| S13 | 已修 | 四个产物统一 `_write_json`（.tmp + `os.replace` 原子替换）；report 与 result 复用同一份排序 |
| S14 | 已修 | `check --allow-extensions` 默认 True，提供 `--no-allow-extensions` 严格模式；run 完直接 check 不再必然失败 |
| S15 | 已修（按确认口径：多币种会出现） | 勾稽按币种分组；支持逐币种总额（`TOTAL USD 100.00` / `TOTAL EUR 50.00` → `totals_by_currency`）；单币种总额但多币种明细时降级 `V-MULTI_CURRENCY` info 并列出各币种合计，绝不跨币种相加 |
| S16 | 已修 | EML 正文费用不再「附件有费用就全丢」：按（费用名 + 金额）与附件结果比对，只并入未覆盖的，并记录 note |
| S17 | 已修 | `{tables_block}` 与 `{hints}` 移入 `<<<DOC 开始>>>` 分隔符内，模型引用表格时 evidence 能锚定 |
| S18 | 已修 | 测试断言改为实际代码 `P-LOW_CONFIDENCE`（原写法用 `or` 同时兼容两种拼写，属于迁就实现） |
| S19 | 已修 | `_clean_party`：`"Name" <mail>` → 只取显示名；显示名本身是邮箱 → 返回 None（不再把 `From` 原文塞进 issuer） |
| S20 | 已修 | `_find_labeled` 取值后 strip 前导 `|`（xlsx 压平成 `key | value` 时会产生） |
| S21 | 已修 | 删除 `REQUIREMENTS_ANALYSIS.md` 歧义表里重复的一行 |

## 9.3 已有问题（2 条，本次不动）

| # | 处置 | 理由 |
|---|---|---|
| 1 | 已补 | 2026-11-01 真机调通并记录真实响应结构；归一层从「找 pages/text」改为「识别 data.list 结构化单据」 |
| 2 | 不修 | 真实数据里没有 Excel/Word，验收 3 的两条能力现在由合成样本 + `test_legacy_formats.py` 覆盖 |

## 9.4 误报（3 条，已进白名单）

规则名 `packet-scope-docs-excluded`：生成评审输入包时排除了 `docs/` 与 `data/`，导致子 Agent 断言「缺文档 / 缺样本」。已确认交付 zip 内实际有 6 个 `.md` 与 12 项样本。

## 9.5 待确认（3 条，已由用户裁定）

| # | 裁定 |
|---|---|
| A7 `.xls/.doc` | 「所有说明的文档都要支持，已有数据必须支持」→ 实现，不做「已知限制」妥协 |
| A2 同单多行相同费用 | 「合法 → 必须保留并标疑」→ 保留 + `V-DUP_LINE`，不再丢弃 |
| S15 多币种 | 「会出现多个币种，要支持」→ 按币种分组勾稽 + 逐币种总额 |

## 9.6 整改后仍需注意

- 真实数据现在 **5 条全部 PENDING_REVIEW**（原 3 条 AP）。这是 A4 的直接结果：那 3 条 AP 本来就依赖 0.6 置信度的推断。若业务上认可「主体名出现在通知方栏即可判为接收方」，应改成显式配置而不是恢复静默推断。
- `.doc` 依赖外部转换器（macOS 自带 textutil；Linux 需 soffice 或 antiword），未内置纯 Python 解析器——`.doc` 是二进制格式，自己实现不划算。机器上没有转换器时会给 `E_DOC_NO_CONVERTER` 并说明安装方式。
