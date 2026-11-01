# 真实数据落地记录（网盘附件 → 系统输出）

> 数据来源：题目「三、输入数据」给的网盘链接（提取码 `pltp`），共 4 个文件。
> 这份文档记录的是**真实数据打脸合成样本**的全过程：改了什么、为什么改、改完结果对不对。

---

## 1. 网盘里到底有什么

> `data/real/` 里是**改名后的副本**（原始中文名在 Windows 解压时容易乱码，原件保留在项目根的 `笔试题AIAGENT/`）：

| `data/real/` 文件名 | 原文件名 | 实际形态 | 关键字段 |
|---|---|---|---|
| `debit_note_DC-108478.pdf` | `PDF 示例账单2.pdf` | NEPTUNE SHIPPING LIMITED（LAX）开给代理的 **DEBIT NOTE**，1 页 | `D/C No. : DC-108478`、`D/C DATE 10-24-2026`、`TOTAL AMOUNT 1,410.40 CURRENCY USD`、3 条费用、`TOTAL 1410.40 0` |
| `arrival_notice_NEPTUNE.pdf` | `PDF示例账单1.pdf` | CMA CGM / NEPTUNE 的 **ARRIVAL NOTICE**，3 页（后 2 页几乎全是免责声明和银行信息） | `CHARGES CUR AMOUNT` 区块里 1 条：`On Carriage Additional - US EmOCA17 USD 245.0000`；`TOTAL CHARGES TO BE PAID: 245.0000` |
| `prealert_SSZSE26093128.eml` | `深圳容宇 PRE-ALERT ....eml` | **PRE-ALERT 邮件**，正文 16 行，带 2 个 PDF 附件 + 1 张内嵌签名图 | 正文一行：`SHA pay TAR：H/C $50`；附件 `4055331281 MBL.pdf`、`4055331281 HBL.pdf`（都是提单） |
| `zim_invoice_scan.png` | `图片示例.png` | **ZIM INVOICE 扫描/截图**，598×841 | `DEBIT INVOICE: ORFR1618731`、`Invoice date 10/03/2025`、2 条 WHARFAGE 费用、总计 `USD 26.90` |

**先说覆盖度**：真实数据里**没有 Excel / Word 样本**。这两条能力目前只由合成样本（`scripts/make_samples.py`）和 `tests/test_ingest.py` 覆盖，文档里不假装它被真实数据验证过。

---

## 2. 第一轮跑出来的样子（改造前）

拿真实数据直接跑，33 条记录全是 `PENDING_REVIEW`，抽出来的"费用"包括：

```
HOUSE B/L NO.        260801188.0   ← 提单号被当成金额
KGS / LBS (G)        34833.03      ← 重量
CNTR. COUNT : 40HC X 2.0           ← 箱量
ROUTING NUMBER ACH   121000358.0   ← 银行路由号
ACCT #               27933123502.0 ← 银行账号
KEARNY, NJ           7032.0        ← 邮编
```

明细合计 `407,851,269.05` vs 单据总计 `1,410.40`，勾稽校验直接把整单拦下。

这就是"合成样本自证清白"的典型症状：我的样本是我按自己的正则写的版式生成的，天然只有我能解析的形态。

---

## 3. 七处真实版式差异与对应改法

| # | 真实现象 | 根因 | 改法（代码位置） |
|---|---|---|---|
| 1 | 单号抽不出来 | 货代用 `D/C No.` 而不是 `Debit Note No` / `DN-xxx` | `REF_PATTERNS` 增加 `d/c no`；单号必须含 3 位以上数字，否则 `EXPORT REFERENCES`、`HOUSE BILL INFO` 会被当成单号（`_find_reference_no`） |
| 2 | 签发方抽成 `YIWU JINBAIREN`（发货人） | Debit Note 的签发方不是 SHIPPER/CONSIGNEE，这俩只是货物关系方 | 四级优先：`balance due to X` → `Issued by/Issuer/Seller` → **跨页重复出现的公司名（抬头）** → `BENEFICIARY`（排除 BANK/SECURITIES 这类收款机构）。`_find_issuer` / `_find_issuer_by_letterhead` |
| 3 | 满页地址、电话、账号被当费用行 | 老实现是**全文档扫行**，没有"费用区块"概念 | 新增 `_charge_regions`：从 `CHARGES CUR AMOUNT` / `DESCRIPTION UNIT RATE QTY P/C DEBIT CREDIT` 这类表头锚点，到 `TOTAL` / `REMARK` / `PLEASE NOTE` / `BENEFICIARY` 结束，区块**不跨页**；只在区块内找费用 |
| 4 | 提单（MBL/HBL）也抽出"费用" | 提单没有费用明细，但满篇重量/地址/邮编 | `_is_transport_document`：出现 `BILL OF LADING / WAYBILL` 且没有费用表头 → 直接不抽费用 |
| 5 | 无锚点文档仍然抽到页码、邮编 | 兜底档太宽 | 兜底改为 `strict=True`：金额没有小数位时，必须同行带币种（`$`/`USD`），否则不认 |
| 6 | 费用名带一堆残留：`HANDLING CHARGE - 65.00 1.00 C` | 真实表格是单空格分隔，`名称 - 单价 数量 P/C 金额`，按 2 空格切列切不开 | `_strip_name_noise`：从尾部剥掉金额/数量/`C`/`PP`/`-` 残渣；行首数量（`4 ADD'L DETENTION...` 里的 4 天）也剥掉 |
| 7 | 币种为 null | 币种只出现在 `CURRENCY USD` 标签行，或金额自带 `$` | `CURRENCY_LABEL_PATTERNS`（`CURRENCY USD` / `USD CURRENCY`）+ `_currency_from_amount`（`$`→USD、`¥`→CNY…） |

另外三处工程性修复：

| 问题 | 修法 |
|---|---|
| EML 正文那条 `H/C $50` 被两个附件各复制一遍 → 2 条重复记录 | `_fill_from_context(include_charges=...)`：正文兜底只在**邮件本体**开启，附件不再重复成单 |
| 附件 `Content-Type: application/octet-stream` + 文件名含 `\xa0`（不换行空格） | 已按文件扩展名分派（`.pdf`），文件名走 `safe_filename` 清洗 |
| 内嵌签名图 `InsertPic_C25B.jpg`（794 KB）被当业务附件送 OCR | `_is_inline_body_image`：`image/*` + 有 `Content-ID` 或 `inline` → 记到 `meta.inline_images`，不解析 |
| 扫描件 OCR 失败但 `errors.json` 是空的（静默丢文件） | 错误快照提前到处理前；处理阶段新增的 `doc.errors`（含附件）统一回写 `errors.json` |
| `10-24-2026` 这种美式日期抽不出 | `rule_dates` 增加 `mdy_num`，首段 >12 时按 DMY 处理 |

---

## 4. 改造后的真实结果（7 条）

```bash
python -m shipping_bill_agent run --input data/real \
  --entity "PltPlt" --entity-alias "湃乐多" --entity-alias "Pilot Aiship" --entity-alias "PLTPLT" \
  --out out/real
```

| 来源 | 单号 | 费用项 | 金额 | 币种 | 单据类型 | 操作 | AR/AP |
|---|---|---|---|---|---|---|---|
| 账单2 | DC-108478 | Handling Charge | 65.00 | USD | Debit Note | PENDING_REVIEW | AP |
| 账单2 | DC-108478 | Detention | 1310.40 | USD | Debit Note | PENDING_REVIEW | AP |
| 账单2 | DC-108478 | Handling Charge | 35.00 | USD | Debit Note | PENDING_REVIEW | AP |
| 账单1 | — | On Carriage Charge | 245.00 | USD | Arrival Notice | PENDING_REVIEW | null |
| EML 正文 | — | Handling Charge | 50.00 | USD | Unknown | PENDING_REVIEW | null |
| 扫描图 | QRF1618731 | Wharfage Charge | 5.00 | null | Invoice | PENDING_REVIEW | null |
| 扫描图 | QRF1618731 | Wharfage Charge | 21.90 | null | Invoice | PENDING_REVIEW | null |

`errors.json` 为空——四份文件全部出结果。

- **明细勾稽**：65 + 1310.40 + 35 = 1410.40 = `TOTAL AMOUNT`，`V-RECONCILIATION` 不再报冲突。这是"区块抽取正确"最硬的证明。
- **AR/AP**：签发方 `NEPTUNE SHIPPING LIMITED` ≠ 主体 `PltPlt`，且单据页脚出现「湃乐多（上海）航运科技有限公司 / Pilot Aiship Tech co., Ltd.」（主体别名）→ 判定为**我方被收费 → AP**。
- **扫描件**：走真实网关 OCR（见 §5），5.00 + 21.90 = 26.90 与单据一致；币种 null 是因为网关 `chargeList` 里没有币种字段。
- **所有记录都是 `PENDING_REVIEW`**：Debit Note 那 3 条虽然判出了 AP，但交易方向依赖「主体别名出现在页脚」的推断（置信度 0.6），
  被 `F-PARTY_INFERRED` blocker 拦下不自动入账。这是 A4 修复的预期结果。
- **其余 `PENDING_REVIEW` 是故意留的**：
  - Arrival Notice 全篇没出现我方名称（收货通知发给 CIMC WETRANS），主体不在交易链上 → 按规则拒绝判向，不硬猜 AP。
  - 邮件是 PRE-ALERT 不是计费单据，没有单据类型 → 不足以判 AR/AP。

---

## 5. 那张图片：现在有结果了（2026-11-01 真机跑通）

`zim_invoice_scan.png`（原 `图片示例.png`）是 ZIM 的 INVOICE 扫描件。这一节之前写的是「为什么没有结果」，
现在改成「怎么拿到结果的」。

### 5.1 之前为什么没有结果

不是网络问题，是 `file` 地址问题。网关要求「OCR 服务端能够访问的下载地址」，本地路径传不了；
而我当时测试用的 `example.com` 之类地址网关下载不到，服务端就卡在下载上直到超时——表现出来像「网络不通」，
把我误导了好几轮（详见 DEV_SPEC §8.5）。

### 5.2 这次怎么跑通的

1. 把 `zim_invoice_scan.png` 传到一个**公网可访问的图床**（uguu），拿到 `https://h.uguu.se/xxxx.png`；
2. 用这个 URL 调 `POST https://gateway.pltplt.com/ai/process`，10.7s 返回 `HTTP 200 / success:true`；
3. 把返回的 JSON 原样存成 `data/ocr_cassettes/f3ca6432d5597b72__orderChange.json`，此后离线也能复现。

### 5.3 真实响应（一手，未修改）

```json
{"data": {"orderType": "ANR_LLM_ZIM-INVOICE",
          "list": [{"fileType": "PAI-ZIM-NOTICE",
                    "invoiceNumber": "QRF1618731",
                    "mblNumber": "ZIMUNGB1312144",
                    "ctrList": [{"ctrNumber": "JXLU4594463", ...}],
                    "chargeList": [{"chargeItem": "WHARFAGE ADMINISTRATION FEE", "amount": "5.00", "chargeCtr": ""},
                                   {"chargeItem": "WHARFAGE", "amount": "21.90", "chargeCtr": ""}],
                    "requiresReview": "N", ...}]},
 "success": true, "message": "文档处理成功"}
```

注意 `chargeList` 的键是 `chargeItem / amount / chargeCtr`——**没有币种字段**。这是网关的能力边界。

### 5.4 管线产出的结果

| 单号 | 费用 | 金额 | 币种 | 操作 |
|---|---|---|---|---|
| QRF1618731 | Wharfage Charge | 5.00 | null | PENDING_REVIEW |
| QRF1618731 | Wharfage Charge | 21.90 | null | PENDING_REVIEW |

**5.00 + 21.90 = 26.90**，与单据总计一致。币种置 `null` 是刻意的：网关没给，我们不从「USIAHALLAM」这类线索硬猜 USD。
`document_date`、`issuer`、`recipient` 同样为 `null`——网关这几个字段返回空，系统不臆造。

### 5.5 与我上一版人工读图的差异（更正）

我上一版文档里用读图方式给过一份「人工核对基准」，现在有 OCR 结果可以对照，发现**我读错了一处**：

| 字段 | 我人工读的 | 网关 OCR | 谁对 |
|---|---|---|---|
| 单号 | ORFR1618731 | **QRF1618731** | 大概率 OCR 把 `O`+`R` 认成了 `Q`；但系统如实采用 OCR 结果，不"纠正"成我读的版本 |
| WHARFAGE 金额 | 26.90 | **21.90** | **OCR 对**。5.00 + 21.90 = 26.90，我把自己看到的 TOTAL 26.90 当成了第二行金额 |
| 总计 | 26.90 | （无该字段） | 一致 |

这条正好说明「人眼看图」不是可靠基准：我会把 TOTAL 串到明细行上去。所以之前那种「人工读出来但不算系统结果」的处理是对的——
现在有了 OCR 就不需要那个基准了。

## 6. 新增的单据类型：Arrival Notice

题目「六、主要字段说明」只约束了 `operation`，没给 `document_type` 的闭集，示例里出现的是 `Debit Note`。真实数据里 1/3 的单据（CMA CGM 到货通知）两者都不是。

处理方式：

- `DOC_TYPES` 增加 `Arrival Notice`（`schema.py`）；
- AR/AP 判定里把它和 `Debit Note` / `Invoice` 同归为「正向计费单据」（签发方向接收方开费用），并在 `ar_ap.py` 里写明理由；
- 不想接受这个扩展的，把 `DOC_TYPES` 改回四个值即可，`--strict-schema` 也不会输出未知类型。

---

## 7. 回归护栏

`tests/test_real_samples.py`（8 条）把上面每一条都钉住了：

```
test_debit_note_three_charges            三条费用金额与顺序
test_debit_note_issuer_is_letterhead_not_shipper   签发方不是发货人
test_debit_note_charges_reconcile_with_total       明细合计 == 总计
test_arrival_notice_uses_charges_block   只取 CHARGES 区块，正文 $25/$1100/$150 不算
test_eml_parses_attachments_and_skips_inline_signature
test_eml_body_charge_not_duplicated      正文费用只出一次
test_real_batch_posting_types            3 条 AP + 2 条 PENDING_REVIEW + 图片进 errors
test_bill_of_lading_attachment_produces_no_charge
```

---

## 8. 那封 PRE-ALERT 邮件：正文、附件、内嵌图分别是怎么回事

这封邮件是四个样本里结构最复杂的一个（1.4MB，一个正文 + 两个 PDF 提单 + 一张 794KB 的内嵌签名图），
单独拆开讲，因为「附件到底有没有被处理」是最容易被怀疑的地方。

### 8.1 三个部分各自的处理方式

| 部分 | 实际内容 | 系统怎么处理 | 产出 |
|---|---|---|---|
| **邮件正文** | 16 行：`ATD/ETA`、`MBL NO.:OOLU4055331281`、`Carrier Free H/C`、`SHA pay TAR：H/C $50`、签名 `湃乐多——中美跨境，数字工具` | 正文当成一张「无版式的单据」跑规则抽取，费用走 `body_charges` 通道 | **1 条**：`Handling Charge 50.00 USD`，`PENDING_REVIEW` |
| **附件 `4055331281 MBL.pdf`** | 80 个文本块，船公司主提单 | 递归解析成子文档；识别为 `BILL OF LADING` → **不抽费用** | 0 条，trace 记 note「识别为提单/运单类非计费单据」 |
| **附件 `4055331281 HBL.pdf`** | 49 个文本块，货代分提单 | 同上 | 0 条，同上 |
| **内嵌图 `InsertPic_C25B.jpg`** | 签名档图片（794KB） | 按 `Content-ID`/`inline` 判为正文内嵌图，**不送 OCR** | 不算独立文件 |

判定内嵌图的那段在 `ingest/eml_parser.py`：只有带 `Content-Disposition: attachment` 或**没有**在正文里被 `cid:` 引用的图片才当附件；
签名图被 `cid:` 引用 → 归入 `meta["inline_images"]`，不进 `children`。

### 8.2 为什么正文只有 1 条、附件 0 条——这是对的

这封邮件是 **PRE-ALERT（到货预告）**，不是计费单据：

- 它只有一句 `SHA pay TAR：H/C $50`（上海付的目的港操作费，50 美元），这是唯一一条真实费用；
- 两份附件都是**提单**，提单上只有 `FREIGHT PREPAID`、重量 `10715.000KGS`、体积 `68.000CBM`、箱号 `TIIU8975975/40HQ`，**没有任何费用明细**。

所以系统抽 1 条、而不是把提单上的重量/箱量/邮编当金额抓出来，正是改造后「先定位费用区块、无锚点不硬抽」的直接结果。
第一轮改造前，这份邮件贡献了 `HOUSE B/L NO. 260801188`、`10715.000` 这类垃圾记录。

### 8.3 但它暴露出三个此前没覆盖的缺口（已修）

这一轮专门验这封邮件时发现：

| # | 问题 | 现象 | 修法 |
|---|---|---|---|
| 1 | **默认 `pdf_strategy="ocr"`** | 两份有文本层的提单**被送去 OCR**，cassette 无对应磁带 → `errors.json` 两条 `E_OCR_CASSETTE_MISS`。此前演示我都显式传了 `--pdf-strategy auto`，把这个问题盖住了 | v1.3.0 定稿：默认保持 `ocr`（题目要求 PDF 走 OCR），OCR 失败自动回退文本层并记 note |
| 2 | **邮件标题不参与抽取** | 标题里有 `CNTR NO.: TIIU8975975`，正文只有 `MBL NO.`，导致 `container_no` 一直抽不到；而箱号是 T4 级匹配的必需键 | `_fill_from_context` 增加「从 subject 补 bl_no / booking_no / container_no」，标 `rule:email_subject`、置信度 ≤0.75；**只补这三个标识字段**，标题里的 `Arrived time` 不会被当单据日期，`VASINGER LLC` 也不会被当交易方 |
| 3 | **提单误报 `E_NO_CHARGE`** | 提单单独喂进来时，因为「解析成功但零费用」被判为错误，`errors.json` 里出现「可能不是账单或版式未被识别」——但提单没费用本来就是预期，这条会淹没真正的失败 | `_looks_like_transport_doc()`：整份输入都是 `BILL OF LADING/WAYBILL` 时改记 note，不进 `errors.json` |

### 8.4 现在单独喂附件会怎样

把 `笔试题AIAGENT/附件提取/` 三个文件当输入跑：

```
4055331281 HBL.pdf   entries=0  ocr=False  note: 识别为提单/运单类非计费单据，未抽取费用（符合预期）
4055331281 MBL.pdf   entries=0  ocr=False  note: 同上
InsertPic_C25B.jpg   entries=0  ocr=False  error: E_OCR_CASSETTE_MISS（单独喂时它只是张图片，只能走 OCR）
```

签名图单独跑报 OCR 失败是合理的——脱离邮件上下文后，系统没有依据判断它是签名图而不是发票扫描件。
在邮件里它不会被送 OCR，这正是「内嵌图」与「附件」要分开的原因。

---

## 9. PDF 的 OCR：跑通了，而且推翻了「OCR 一定更好」的直觉

上一版本文档里我写的是「PDF 的真实 OCR 未做」。这一节把它补上，顺带记录一个反直觉的结论。

### 9.1 之前为什么卡住

`file` 必须是网关自己能下载的地址。图片好办（uguu 图床），PDF 就没地方放：

| 服务 | 结果 |
|---|---|
| uguu.se | `415 Filetype not allowed`（只收图片） |
| 0x0.st | `503 uploads disabled`（官方关闭，原因是 AI 爬虫滥用） |
| catbox / litterbox / tmpfiles / gofile / file.io / pixeldrain / bashupload | 连接 000，不通 |
| temp.sh | 能上传，但下载链接要 POST 表单，网关只会 GET |
| **paste.c-net.org** | ✅ `--data-binary @file`，下载回来 **shasum 完全一致**、`application/pdf`、无重定向 |

> 顺带一个坑：**filebin.net 看着能用，其实不能**。它上传返回 201、自己下载也正常，
> 但网关拿到短链后返回 `page_index: [0,2]`——而这份 PDF 只有 1 页，说明它读的不是这个文件；
> 换成它给出的 S3 签名直链，网关直接 504。**「我能下载」≠「网关能下载」。**

### 9.2 四份 PDF 都跑了真实网关

| 文件 | fileType | 单号 | 提单号 | 箱号 | 抽到的费用 |
|---|---|---|---|---|---|
| `debit_note_DC-108478.pdf` | `PAI-COLOADER-NOTICE` | **空** | MAEU275752922 | MRSU2571577 / TCLU8619685 | HANDLING CHARGE 100.00、4 ADD'L DETENTION FREE TIME CHARGE 1310.40 |
| `arrival_notice_NEPTUNE.pdf` | `PAI-CMA-NOTICE` | **空** | CMDUCHN4053601 | SAPU2040689 | On Carriage Additional - US Em 245.00 |
| `4055331281 MBL.pdf` | 空 | 空 | 空 | 空 | 空 |
| `4055331281 HBL.pdf` | 空 | 空 | 空 | 空 | 空 |

四份 PDF 的真实响应全部存成了 cassette（`data/ocr_cassettes/`），离线可复现，`pytest` 里有测试守着。

### 9.3 反直觉的部分：对这份账单，OCR 比文本层差

同一份 Debit Note，两条路径的结果摆一起：

| 字段 | 文本层 | 网关 OCR | 谁更好 |
|---|---|---|---|
| 单号 | `DC-108478` | 空（`invoiceNumber: ""`） | **文本层** |
| 币种 | USD | 空（`chargeList` 里根本没有币种字段） | **文本层** |
| 单据类型 | Debit Note | `PAI-COLOADER-NOTICE`（网关自家分类） | **文本层** |
| 明细 | 65.00 + 1310.40 + 35.00（**3 条**） | 100.00 + 1310.40（**2 条**） | **文本层** |
| 合计 | 1410.40 | 1410.40 | 都正确 |
| 提单号 | MAEU275752922 | MAEU275752922 | 平 |
| 箱号 | MRSU2571577 | MRSU2571577 + **TCLU8619685** | **OCR 更全** |

两个关键差异：

1. **OCR 把 65.00 和 35.00 合并成了一条 100.00。** 总额没错（100 + 1310.40 = 1410.40），
   但「哪笔是操作费、哪笔是附加操作费」的明细粒度没了。账单入账要的是明细，总额对不对不是唯一标准。
2. **OCR 没抽到单号，也没有币种。** 单号是 T1 级匹配的必需键；没有单号，同一张单会被判成 CREATE 重复入账。

所以直觉上「OCR 是更先进的路径」在这份数据上不成立。**OCR 的价值不在替代文本层，而在补文本层缺的东西**——
比如箱号，网关一次给了两个（文本层只抠出一个）。

### 9.4 于是策略改成：文本层为主源，OCR 只补标识

| 文档 | OCR 角色 | 行为 |
|---|---|---|
| PDF 有文本层 | **supplement** | 照样调网关（题目要求 PDF 走 OCR），结果放副源位，**不覆盖**文本层；抽取后只补 `bl_no / booking_no / container_no / reference_no` |
| 扫描件（无文本层） | **primary** | OCR 是唯一来源 |
| OCR 失败 + 有文本层 | fallback | 沿用文本层，记 note，不进 `errors.json` |
| OCR 失败 + 无文本层 | 失败 | 进 `errors.json`，零记录 |

故意**不合并**的三类字段，理由写在 `src/shipping_bill_agent/extract/ocr_merge.py` 的模块文档里：

- `document_type`：网关的 `PAI-COLOADER-NOTICE` / `PAI-CMA-NOTICE` 是它自家分类，映射到标准单据类型不可靠；
- `issuer` / `recipient`：网关的 `shipper` / `consignee` 是**货物关系方**。Debit Note 的 `consignee` 是
  `AEBO TCO IMPORT LLC`，既不是出单人也不是付款人，拿来判 AR/AP 会判错；
- 金额 / 币种 / 日期：文本层能逐字回溯原文，OCR 有识别误差，冲突时文本层优先。

### 9.5 改完的结果

真实数据仍是 **7 条记录**，与纯文本层版本**逐字段一致**，`ocr_calls=5 / ocr_errors=0`（之前是 4 次失败），
额外补到了 Arrival Notice 的 `bl_no = CMDUCHN4053601`。

trace 里每一份 PDF 都有明确记录：

```
OCR 已执行；该 PDF 有文本层，以文本层为主源，OCR 仅补缺失标识：debit_note_DC-108478.pdf
OCR 补充字段：bl_no（arrival_notice_NEPTUNE.pdf）
```

这样「PDF 走没走 OCR」不用猜，看 trace 就有答案；「哪条数据来自 OCR」看 provenance 的 `ocr:supplement` 也能定位。
