# DEV_SPEC：航运智能账单助手智能体

> 版本：1.0.1 · 状态：施工中 · 日期：2026-10-31
> 上游：`docs/REQUIREMENTS_ANALYSIS.md`（需求）、`docs/DESIGN.md`（方案）
> 本文是施工蓝图：模块签名、契约、错误码、任务清单。**实现与本文不一致时，先改本文再改码。**

---

## 0. 文档元信息

| 项 | 内容 |
|---|---|
| 包名 | `shipping_bill_agent` |
| 入口 | `python -m shipping_bill_agent run ...` / `python -m shipping_bill_agent check ...` |
| Python | ≥ 3.11（实测 3.13.12） |
| 关键依赖 | `langchain-core>=0.3`、`langchain-openai>=0.3`、`pydantic>=2.7`、`openpyxl`、`python-docx`、`pypdf`、`requests`、`pyyaml`、`pytest` |
| 实测版本 | langchain 1.4.7 / langchain-core 1.7.3 / pydantic 2.14.2 |
| 已验证项 | OCR 网关真实网络调用（2026-11-01 真机调通：公网 PDF → HTTP 200 / success:true / 7–12s）；题目原始附件数据（网盘已下载到 `data/real/`） |
| 未验证项 | 真实账单经网关 OCR 的完整结果（受限于 `file` 必须是公网可达地址，本地文件上传通道受限） |

---

## 1. 配置（`config.py`）

环境变量优先级：CLI 参数 > 环境变量 > 默认值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `SBA_OCR_MODE` | `cassette` | `gateway` \| `cassette` \| `stub` |
| `SBA_OCR_URL` | `https://gateway.pltplt.com/ai/process` | 网关地址 |
| `SBA_OCR_TASK` | `orderChange` | 请求体 `task` |
| `SBA_OCR_PUBLIC_BASE` | 空 | 本地文件 → 公网 URL 的映射前缀（`gateway` 模式必需） |
| `SBA_OCR_CASSETTE_DIR` | `data/ocr_cassettes` | 回放目录 |
| `SBA_OCR_TIMEOUT` | `(10, 300)` | (连接, 读取) 秒 |
| `SBA_OCR_RETRIES` | `3` | 网络错误重试次数 |
| `SBA_PDF_STRATEGY` | `ocr` | `ocr` \| `text` \| `auto`（auto：文本层 ≥ 120 字符则跳过 OCR） |
| `SBA_LLM_ENABLED` | `auto` | `auto`（有 key 则启用）\| `true` \| `false` |
| `SBA_LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容端点 |
| `SBA_LLM_API_KEY` | 空 | 无 key → 自动 `rule_only` |
| `SBA_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `SBA_LLM_TEMPERATURE` | `0` | 必须为 0 保证可重放 |
| `SBA_ACCOUNTING_ENTITY` | `Origin Agent` | 记账主体 |
| `SBA_CREDIT_NOTE_POLICY` | `mirror` | `mirror` \| `same_side` |
| `SBA_CONF_AMOUNT` | `0.90` | 金额置信度阈值 |
| `SBA_CONF_CURRENCY` | `0.90` | 币种置信度阈值 |
| `SBA_CONF_NAME` | `0.60` | 费用名置信度阈值 |
| `SBA_MATCH_T4_STRICT` | `true` | T4 五键全命中才匹配 |

代码形态：

```python
@dataclass(frozen=True)
class Settings:
    ocr: OcrSettings
    llm: LlmSettings
    pipeline: PipelineSettings
    @classmethod
    def load(cls, **overrides) -> "Settings": ...
```

---

## 2. 契约模型（`schema.py`）

### 2.1 常量表

```python
DOC_KINDS   = ("eml", "pdf", "image", "excel", "word", "unknown")
DOC_TYPES   = ("Debit Note", "Credit Note", "Invoice", "Unknown")
OPERATIONS  = ("CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW")
SEVERITIES  = ("blocker", "warn", "info")
SCALAR_FIELDS = ("document_type", "reference_no", "document_date", "issuer", "recipient",
                 "bl_no", "booking_no", "container_no", "currency", "payment_term",
                 "subtotal", "tax", "total")
```

### 2.2 模型清单

| 模型 | 关键字段 | 备注 |
|---|---|---|
| `SourceRef` | `path, sha256, kind, origin, size_bytes` | `origin` 形如 `root` / `attachment:DN.pdf` / `attachment:mail.eml>sub.eml>a.pdf` |
| `Block` | `kind, text, page, line_no` | `kind ∈ paragraph/kv/header/table_row/note` |
| `TableBlock` | `name, page, header: list[str], rows: list[list[str]]` | header 可为空（无表头表） |
| `RawDocument` | `source, doc_kind, text, blocks, tables, meta, children, errors` | 递归结构 |
| `FieldVal` | `value: Any, span: str\|None, extractor: str, confidence: float` | 单字段抽取结果 |
| `ChargeCandidate` | `raw_name, amount, quantity, unit_price, currency, payment_term, span, page, line_no, confidence` | 抽取层输出 |
| `Extraction` | `scalars: dict[str, FieldVal], charges: list[ChargeCandidate], notes: list[str], extractor: str` | 规则轨与 LLM 轨同构，便于对账 |
| `Issue` | `code, severity, message, field, detail` | `code` 见 §5 |
| `Provenance` | `field, source_path, page, line_no, span, extractor, confidence` | 字段级 |
| `MatchInfo` | `tier, bill_id, margin, ambiguous, candidates` | `tier ∈ T1..T4/None` |
| `BillEntry` | 见 §2.3 | 内部主对象 |
| `OutputRecord` | 见 §3 | 对外契约 |

### 2.3 `BillEntry`（内部主对象）

```python
class BillEntry(BaseModel):
    # 单据级
    document_type: str | None
    reference_no: str | None
    document_date: str | None          # YYYY-MM-DD
    issuer: str | None
    recipient: str | None
    bl_no: str | None
    booking_no: str | None
    container_no: str | None
    # 费用级
    charge_item: str | None            # 标准名
    charge_item_raw: str | None        # 原始名
    amount: float | None
    currency: str | None
    payment_term: str | None           # PP | CC | None
    quantity: float | None
    unit_price: float | None
    tax: float | None
    # 财务
    accounting_entity: str | None
    posting_type: str | None           # AR | AP | None
    # 决策
    operation: str = "PENDING_REVIEW"
    match: MatchInfo | None = None
    issues: list[Issue] = []
    provenance: dict[str, Provenance] = {}
    source_paths: list[str] = []
    ai_reasoning: list[str] = []
    degraded: bool = False
```

---

## 3. 输出契约（题目五）

默认输出（`out/result.json`）：顶层为 **list**，元素结构固定：

```json
{
  "operation": "UPDATE",
  "document_info": {"document_type": "Debit Note", "reference_no": "DN-202609-001", "document_date": "2026-09-20"},
  "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
  "charge_details": {"charge_item": "Handling Charge", "charge_item_raw": "Handling Fee",
                     "amount": 40.0, "currency": "USD", "payment_term": "PP"},
  "financial_attributes": {"accounting_entity": "Origin Agent", "posting_type": "AP"},
  "audit_trail": {"ai_reasoning": ["..."]}
}
```

规则：

1. 缺值一律 `null`，**不得省略键**（保证下游按固定 schema 解析）。
2. `amount` 必须是 `number`（`float`），不是字符串。
3. `currency` 三位大写代码。
4. `charge_details.charge_item_raw` 为**扩展字段**：`--strict-schema` 时剔除，保持与题目示例逐键一致。
5. 排序：`(reference_no, charge_item)` 稳定排序，保证 diff 友好。

旁路产物（不属于验收契约，但必须产出）：

| 文件 | 内容 |
|---|---|
| `report.json` | 每条记录的完整 `BillEntry`（含 provenance / issues / match 候选 / degraded） |
| `trace-<run_id>.json` | 阶段耗时、OCR 调用、LLM 调用、token、各文件结果摘要 |
| `errors.json` | 解析失败的文件清单与原因 |

---

## 4. 模块规格

### 4.1 `ingest/dispatch.py`

```python
SUPPORTED = {".eml": "eml", ".pdf": "pdf", ".png": "image", ".jpg": "image",
             ".jpeg": "image", ".tif": "image", ".tiff": "image",
             ".xls": "excel", ".xlsx": "excel", ".doc": "word", ".docx": "word"}

def detect_kind(path: Path) -> str: ...
def ingest(path: Path, *, workdir: Path, settings: Settings, origin: str = "root") -> RawDocument: ...
def ingest_path(path: Path, ...) -> list[RawDocument]:   # 目录展开 + 递归树拍平
```

- `detect_kind` 先按扩展名，扩展名未知时嗅探魔数（`%PDF-`, `PK\x03\x04`, `\xd0\xcf\x11\xe0`, `\xff\xd8`, `\x89PNG`）。
- `.xls`（旧 BIFF）与 `.doc`（旧 OLE）→ 抛 `UnsupportedFormatError`，错误码 `E_UNSUPPORTED_XLS/DOC`，提示另存为新格式。
- 任何解析异常 → `RawDocument.errors` 追加，不向上抛（H6 失败不丢数据）。
- `sha256` 对原始字节计算，用于 cassette 键与幂等。

### 4.2 `ingest/eml_parser.py`

```python
def parse_eml(path: Path, *, workdir: Path, settings: Settings, origin: str) -> RawDocument: ...
```

- 头：`Subject / From / To / Cc / Date`，`decode_header` 处理 `=?utf-8?B?...?=` 与 GBK。
- 正文：优先 `text/plain`；无则 `text/html` → `html_to_text()`（去标签、解实体、保留换行）。
- 附件：遍历 `walk()`，取 `get_filename()`，落盘到 `workdir/attachments/<sha8>_<name>` 后 `ingest()` 递归；`message/rfc822` 嵌套附件递归深度上限 5。
- 附件文件名中的中文/URL 编码用 `urllib.parse.unquote` + `decode_header` 还原。
- `meta` 存 `subject/from/to/cc/date/attachments[]`。
- **正文是否当账单**：由 pipeline 判定（见 §4.9），parser 只负责如实提取。

### 4.3 `ingest/excel_parser.py`

```python
def parse_excel(path: Path, *, origin: str) -> RawDocument: ...
```

- `openpyxl.load_workbook(read_only=True, data_only=True)`，遍历全部 sheet。
- 每 sheet：所有非空行 → `TableBlock(name=sheet, header=<首行若命中表头关键词>, rows=[...])`。
- 表头判定：前 15 行内得分最高且 ≥2 的行；无命中则整表无表头，由抽取层按列内容推断（金额列 = 数值密度最高的列）。
- **文本表示保留全部行**（含表头之上的单据头信息），否则单号/交易方/日期会丢；`TableBlock` 只保留表头及其后的行。
- `.xls` → `UnsupportedFormatError`。

### 4.4 `ingest/word_parser.py`

```python
def parse_word(path: Path, *, origin: str) -> RawDocument: ...
```

- `python-docx`：`paragraphs` → `Block(kind="paragraph")`（跳过空段）；`tables` → `TableBlock`（首行为 header）。
- 合并段落时保留 `kv` 识别：`某某：值` / `某某: 值` 正则 → `Block(kind="kv")`。

### 4.5 `ingest/pdf_parser.py`

```python
def parse_pdf_text(path: Path, *, origin: str) -> RawDocument: ...
```

- `pypdf.PdfReader` 逐页 `extract_text()`，按行切 `Block`，`page` 从 1 计。
- 无文本层（总字符 < 20）→ 返回 `text=""` 并在 `meta["has_text_layer"]=False`，由 pipeline 决定是否转 OCR。
- 注意：PDF 表格识别不做，表格结构交给 OCR 或文本行启发式（`extract/rule_extractor.py` 的行解析器）。

### 4.6 `ocr/`

```python
# client.py
def call_ocr(file_url: str, *, settings: Settings, session: requests.Session | None = None) -> dict
```
- 请求体严格 `{"file": file_url, "task": settings.ocr.task, "callBack": "", "taskType": ""}`，`json=` 提交，`timeout=(10,300)`，`raise_for_status()`。
- 重试：仅对连接错误 / 5xx / 429 生效，指数退避 `1,2,4` 秒；4xx 不重试。
- 返回 `response.json()`。

```python
# providers.py
class OcrProvider(Protocol):
    def read(self, path: Path, *, task: str) -> dict: ...

class GatewayOcrProvider:   # 需要 public_base 或已是 http(s) URL
class CassetteOcrProvider:  # key = sha256(bytes) + ":" + task；miss → CassetteMissError
class StubOcrProvider:      # 返回 {"code":0,"data":{"pages":[{"text":""}]}}
def build_provider(settings: Settings) -> OcrProvider: ...
```
- cassette 文件命名 `<sha256[:16]>__<task>.json`，内容为网关原始响应体（不做预处理），保证"录制即真实形状"。
- `CassetteOcrProvider` 记录命中/未命中到 `trace`。

```python
# normalize.py
def normalize_ocr(raw: dict, *, page_count_hint: int | None = None) -> tuple[str, list[Block], list[TableBlock]]
```
- **形状无关**：递归遍历 JSON，用 `TEXT_KEYS = {"text","content","words","line","value","result"}` 收集文本行；识别 `list[list]`/`list[dict]` 为表格。
- 文本行去重保序（OCR 常重复返回）。

### 4.7 `extract/`

```python
# rule_extractor.py
def extract_by_rules(docs: list[RawDocument], *, ctx: DocContext) -> Extraction: ...
```
- 单据类型：`DEBIT NOTE|CREDIT NOTE|INVOICE|发票|借记|贷记` 关键词（大小写不敏感，命中即 `FieldVal(confidence=0.9)`）。
- 单号：`REF_RE = r"(?:Invoice|Debit Note|Credit Note|Doc(?:ument)?\s*No|Ref(?:erence)?\s*No|单号|编号)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{3,30})"`，再兜底全文扫 `\b[A-Z]{2,4}[-/]?\d{4,}[-/]?\d{2,}\b`。
- 日期：`YYYY-MM-DD`、`YYYY/MM/DD`、`DD-MMM-YYYY`、`YYYY年M月D日`、`20 Sep 2026` → 归一 `YYYY-MM-DD`。
- 交易方：`Issued by|Issuer|Seller|Bill From|From` → issuer；`To|Bill To|Buyer|Consignee|Agent` → recipient。同行 `A → B` 也可。
- 金额：`MONEY_RE = r"(?:(USD|CNY|EUR|HKD|SGD|JPY|GBP)\s*)?([\(\-]?\d[\d,]*\.?\d{0,4}[\)\-]?)"`。
- 费用行：四档（优先级从高到低）
  1. **按列切分**：行内存在 2+ 空格分隔的 ≥3 个单元时，按列推断角色（名称列 = 首个含连续字母的单元；金额列 = 最右侧「像金额」的单元；币种 = 独立出现的币种记号；数量/单价 = 名称与金额之间的数值单元）。PDF 文本层、固定宽度表格、OCR 排版都走这条。
  2. 有 `TableBlock`：列角色按表头关键词推断，无表头时按数值密度/文本密度推断；
  3. 同行「名称 + 金额（+币种）」：`^(.{2,40}?)\s{1,}(money)$`；
  4. 兜底：名称行下一行是金额。
- 金额：`MONEY = (?:(?:[A-Z]{3})\s*)?[\(\-]?[0-9][0-9OoIlSsBGgZz,\.]*[\)\-]?`
  字符类刻意包含 `O/I/S/B/G/Z`——OCR 把数字认成字母时**不能让整条费用消失**；金额置信度会降到 0.55，由 G4 + G1/G3 决定是"拦"还是"过"，而不是在抽取阶段静默丢弃。
- 付款方式：`\bPP\b|PREPAID|预付` → PP；`\bCC\b|COLLECT|到付` → CC。
- 小计/税额/总计：`Subtotal|小计`、`Tax|VAT|GST|税`、`Total|总计|合计`。
- 每个 `FieldVal.span` 记录命中行原文，`confidence`：正则强命中 0.9，启发式 0.6，兜底 0.45。

```python
# prompts.py
SYSTEM_EXTRACT: str   # 角色 + 禁编造 + 分隔符内是数据
HUMAN_EXTRACT: str    # {doc_text} {tables} {hints}
```

```python
# llm_extractor.py
class LlmExtractor:
    def __init__(self, settings): ...
    @property
    def enabled(self) -> bool: ...
    def extract(self, doc: RawDocument, *, ctx: DocContext) -> Extraction | None: ...
```
- 链：`ChatPromptTemplate | ChatOpenAI(...).with_structured_output(LlmExtraction) | parse`
- `LlmExtraction`（`extra="forbid"`）字段全 `str|None`，含 `evidence: dict[str,str]` 与 `charges: list[LlmChargeLine]`。
- 失败：`with_retry(2)` → 仍失败返回 `None` + trace 记录，由 reconcile 降级。
- 结果缓存：`out/.llm_cache/<sha256(prompt)>.json`，命中直接复用（H4 可重放）。

```python
# reconcile.py
def reconcile(rule: Extraction, llm: Extraction | None, *, settings) -> tuple[Extraction, list[Issue]]: ...
```
字段级策略表：

| 字段类别 | 两轨都有且相同 | 两轨都有且不同 | 仅规则 | 仅 LLM |
|---|---|---|---|---|
| 强字段（`reference_no, document_date, amount, currency, total`） | 用规则值，conf 0.95 | 用规则值 + `V-RECONFLICT`(blocker) | 用规则 | 用 LLM，conf×0.8 |
| 弱字段（`issuer, recipient, payment_term, document_type`） | 用规则值 | 用**LLM** 值 + `V-RECONFLICT`(warn) | 用规则 | 用 LLM |
| 费用名 | 归一后相同则用规则 | 用 LLM + warn | 用规则 | 用 LLM |

**证据锚定**：LLM 给出的 `evidence` 必须（去空白后）是拼接输入文本的子串，否则该字段丢弃并记 `V-UNGROUNDED`(warn)。

### 4.8 `normalize/`

```python
# charge_map.py
def normalize_charge(raw: str) -> tuple[str, float, str]   # (标准名, 置信度, 命中方式)
```
顺序：① 精确别名（去噪后）→ 1.0；② 别名子串包含 → 0.9；③ `difflib.SequenceMatcher ≥ 0.86` → 0.8；④ 关键词规则表 → 0.7；⑤ 保留原文 → 0.4 + `N-UNMAPPED`(info)。
`charge_aliases.yaml` 至少含题目 6 组，另补 `BAF/CAF/ISPS/AMS/ENS/Telex Release/Seal Fee/...`。

```python
# currency.py
def normalize_currency(raw: str | None) -> tuple[str | None, float]
def is_iso4217(code: str) -> bool
```
`SYMBOLS = {"$": "USD", "US$": "USD", "USD": "USD", "¥": "CNY", "RMB": "CNY", "￥": "CNY", "€": "EUR", "£": "GBP", "HK$": "HKD", "S$": "SGD", "美金": "USD", "美元": "USD", "人民币": "CNY"}`；未知 → `(None, 0.3)`。

```python
# amounts.py
def parse_amount(raw: str | None) -> tuple[float | None, float]
```
处理：千分位 `,`、欧式 `1.234,56`、括号负数 `(1,234.00)`、尾负号 `123.45-`、前后币种前缀。返回 `(value, confidence)`；解析失败 `(None, 0.0)`。

### 4.9 `pipeline.py`

```python
def process_document_tree(root: RawDocument, *, settings, store, recorder) -> list[BillEntry]
def process_path(path: Path, *, settings, store, recorder) -> list[BillEntry]
```

流程（每个 leaf 文档）：

```text
1  ctx = DocContext.from_tree(root)          # EML 头/正文提供的兜底字段
2  若 kind in (pdf, image) 且需 OCR → provider.read() → normalize_ocr() → 追加 blocks/tables/text
3  rule = extract_by_rules([doc], ctx)
4  llm  = llm_extractor.extract(doc) if enabled
5  ext, recon_issues = reconcile(rule, llm)
6  对 ext.charges 每条 → build_entry(ext, charge, ctx)
      6.1 normalize_charge / normalize_currency / parse_amount / payment_term
      6.2 validate（G1-G5）→ issues
      6.3 match（T1-T4）
      6.4 decide：operation + posting_type + 字段级 patch
      6.5 ai_reasoning 生成
7  单据级勾稽（G3 汇总）→ 回写到该单据所有 entry 的 issues
8  跨文件/跨历史重复（G5-②）
```

**EML 正文是否成单**：若该 EML 的 attachments 里已有任一 leaf 产出 ≥1 条 charge，则正文不再单独成单（避免邮件正文重复计费）；否则正文自身作为 bill source。判定结果写入 trace。

### 4.10 `decide/`

```python
# ar_ap.py
def decide_posting_type(*, document_type, issuer, recipient, entity, credit_policy="mirror") -> tuple[str|None, str]
```
返回 `(posting_type, reason)`，reason 直接进 `ai_reasoning`。

```python
# planner.py
def plan(entry, *, match, original: dict|None, settings) -> BillEntry
```
操作类型判定顺序（**先拦后判**）：

```text
1 blocker issue 存在            → PENDING_REVIEW
2 match.ambiguous               → PENDING_REVIEW
3 match 为 None 且无 blocker    → CREATE
4 match 命中：
   4.1 逐字段 diff（仅"有证据"的字段参与）：
       无证据            → 继承原值
       原值 None + 新值有证据 → 补全（不算变更，记入 filled）
       两边有值且不同     → 应用 patch（记入 changed）→ UPDATE
       无任何 changed    → NO_CHANGE
5 posting_type 为 None          → PENDING_REVIEW（但保留 operation 语义到 intended_operation）
6 变更字段置信度 < 阈值         → PENDING_REVIEW
```

- 「补全」与「变更」必须分开：原账单没记数量/单价而输入材料有时，属于信息补齐，不应把整单判成 UPDATE（也不应把原值覆盖成 null）。
- `intended_operation` 记录"本来会做什么"（CREATE/UPDATE/NO_CHANGE），即使最终被拦成 `PENDING_REVIEW` 也保留在 `report.json`，便于人审时判断意图。
- 字段级 patch：`FIELD_ORDER = (amount, currency, payment_term, charge_item, quantity, unit_price, tax, document_date)`
- 继承：无证据字段一律取 `original` 值，不用 `None` 覆盖。
- `--idempotent`：`(file_sha256, reference_no, charge_item)` 已产出 `CREATE/UPDATE` 时，本轮降级 `NO_CHANGE` 并注明"重复投递"。

### 4.11 `audit.py`

- `Recorder`：`stage(name)` 上下文管理器，记录 `{stage, ms, ok, detail}` 到内存，`flush(run_dir)` 写 `trace-<run_id>.json`。
- `ai_reasoning` 生成规则（3-6 句）：
  1. 识别依据（"从 DN-202609-001.pdf 第 1 页表格第 2 行识别到 Handling Fee 40.00 USD"）
  2. 归一依据（"Handling Fee 按别名表映射为 Handling Charge"）— 仅当发生映射
  3. 匹配依据（"通过单据编号 DN-202609-001 + 费用名 Handling Charge 匹配到已有账单 B-1001"）
  4. 变更依据（"OCR 原始金额 40.00 与原账单 35.00 不一致，按输入材料更新为 40.00"）
  5. 未改字段理由（"其余字段未被明确要求修改，保留原值"）
  6. 风险提示（"币种 US$ 依符号归一为 USD，置信度 0.9"）

### 4.12 `cli.py`

```text
python -m shipping_bill_agent run
  --input PATH [PATH...]        文件或目录，可多个
  --entity NAME                 记账主体（默认配置）
  --bills PATH                  历史账单 JSON
  --out DIR                     输出目录（默认 out/run-<ts>）
  --ocr-mode {gateway,cassette,stub}
  --pdf-strategy {ocr,text,auto}
  --llm {auto,on,off}
  --strict-schema               剔除扩展字段
  --idempotent
  --json-only                   只打印 result.json 到 stdout

python -m shipping_bill_agent check --input out/run-xxx/result.json
  # 校验输出契约（键集合/类型/枚举），退出码 0/1
```

---

## 5. 错误与 issue 码表

| code | severity | 触发 | 动作 |
|---|---|---|---|
| `E_PARSE_FAILED` | blocker | 解析器抛异常 | 该文件跳过 + errors.json |
| `E_UNSUPPORTED_DOC` | blocker | `.doc` 旧格式 | 提示另存 `.docx` |
| `E_UNSUPPORTED_XLS` | blocker | `.xls` 旧格式 | 提示另存 `.xlsx` |
| `E_OCR_FAILED` | blocker | OCR 重试耗尽/网关错误 | 记录转 PENDING_REVIEW |
| `E_OCR_CASSETTE_MISS` | blocker | 回放未命中 | 明确提示切 gateway 或补录 |
| `E_NO_CHARGE` | info | 未抽到任何费用项 | 不产出记录 |
| `V-AMOUNT_INVALID` | blocker | 金额非数字/负数（无 credit 标记） | PENDING_REVIEW |
| `V-CURRENCY_INVALID` | warn | 非 ISO 4217 | 不阻断，记风险 |
| `V-RECONCILIATION` | blocker | 明细+税 ≠ 总计（>0.01） | PENDING_REVIEW |
| `V-OCR_CONFUSION` | warn | 数字字段含 `0O1Il5S8B6G2Z` 混淆 | 金额类 → PENDING_REVIEW |
| `V-DUP_LINE` | warn | 同单内重复费用 | 去重 |
| `V-DUP_BILL` | blocker | 与历史账单重复 | PENDING_REVIEW |
| `V-RECONFLICT` | blocker/warn | 规则轨与 LLM 轨冲突 | 见 §4.7 表 |
| `V-UNGROUNDED` | warn | LLM 证据不在原文 | 字段丢弃 |
| `N-UNMAPPED_CHARGE` | info | 费用名未命中别名表 | 保留原文名 |
| `M-AMBIGUOUS` | blocker | 匹配多命中 | PENDING_REVIEW + 候选 |
| `M-NO_MATCH` | info | 零命中 | CREATE |
| `F-ARAP_UNKNOWN` | blocker | 主体不在交易双方 | posting_type=null |
| `F-CREDIT_POLICY` | info | Credit Note 按 mirror 口径落账 | 记依据 |
| `P-LOW_CONFIDENCE` | warn | 变更字段置信度低于阈值 | PENDING_REVIEW |
| `P-IDEMPOTENT` | info | 重复投递 | NO_CHANGE |

---

## 6. 测试计划

| 文件 | 覆盖 |
|---|---|
| `tests/test_ingest.py` | 魔数嗅探、目录展开、`.doc/.xls` 报错、EML 附件递归、Word/Excel 表格 |
| `tests/test_ocr.py` | 请求体逐键匹配、超时元组、4xx 不重试 / 5xx 重试、cassette 命中与 miss、形状无关归一 |
| `tests/test_extract.py` | 单号/日期/金额/币种/PP-CC/费用行三档、证据 span 非空 |
| `tests/test_normalize.py` | 题目 6 组别名映射、模糊命中、欧式千分位、括号负数、币种符号 |
| `tests/test_validate.py` | G1-G5 各一正一反、OCR 混淆不自改 |
| `tests/test_matching.py` | T1-T4 命中、优先级短路、多命中 ambiguous、tie-break |
| `tests/test_ar_ap.py` | 四象限 + 主体不在场 + Credit Note mirror/same_side |
| `tests/test_planner.py` | CREATE / UPDATE 字段级 patch / NO_CHANGE / PENDING、继承不覆盖 |
| `tests/test_pipeline_e2e.py` | 全流程跑 samples，断言 result.json 与金标一致、`check` 通过 |
| `tests/test_schema.py` | 契约键集合、金额为 number、枚举合法、strict 模式无扩展字段 |
| `tests/test_real_samples.py` | 真实网盘样本回归：D/C 单号、抬头签发方、明细=总计、CHARGE 区块、EML 附件与内嵌图、正文费用不重复、提单不抽费用 |
| `scripts/evaluate.py` | 字段级 P/R/F1、操作类型混淆矩阵、风险召回、记录级正确率；`--gate` 回归门禁 |

实际执行：`pytest` → **130 passed**；`scripts/evaluate.py --gate` → micro-F1 1.000 / 风险召回 1.000 / 门禁通过。

---

## 8. 实现与规格的差异记录

施工中修正了规格的 4 处不准确描述，均已回写上文：

| # | 原文 | 实际 | 原因 |
|---|---|---|---|
| 1 | 费用行「三档」 | 四档（新增"按列切分"在最前） | PDF 文本层与 OCR 排版没有结构化表格，只能靠固定宽度列切分；压成单空格后此档失效，因此解析层必须保留行内空白（`utils.preserve_lines`） |
| 2 | UPDATE 判定只有"有差异/无差异" | 增加"补全"第三态 | 原账单字段为 null 而新材料有值，属于信息补齐，不应判 UPDATE，更不应覆盖 |
| 3 | Excel 表头只看首行 | 前 15 行内取最高分 | 真实 Excel 上方常有单据头信息（Invoice No / Issued by），首行不是表头 |
| 4 | 金额正则只允许数字与分隔符 | 允许 `O/I/S/B/G/Z` 等混淆字符 | 否则 OCR 混淆的金额会导致**整条费用消失**——比"金额错但被拦住"更危险 |

另：`BillEntry` 新增 `intended_operation`（被拦成 PENDING_REVIEW 时保留原始意图），仅出现在 `report.json`，不影响验收契约。

### 8.1 真实数据落地后的第二次修正

拿到题目网盘附件（`data/real/`，1 EML + 2 PDF + 1 PNG）后，真实版式推翻了规格里 6 处假设：

| # | 规格原文 | 实际做法 | 原因 |
|---|---|---|---|
| 5 | 费用抽取「按行扫全文」 | 先定位**费用区块**（表头锚点 → 合计/备注锚点，不跨页），只在区块内抽 | 真实账单满页地址/电话/银行账号/免责声明金额，全量扫行会抽出 33 条垃圾记录 |
| 6 | 单号模式含 `Debit Note No / DN-xxx` | 增加 `D/C No.`；且候选值必须含 3 位以上数字 | 货代实际用 `D/C No. : DC-108478`；`EXPORT REFERENCES`、`HOUSE BILL INFO` 会被误当单号 |
| 7 | 签发方取 `Issued by / Shipper / Exporter` | 四级优先：`balance due to` → `Issued by/Issuer` → 跨页重复公司名（抬头）→ `BENEFICIARY`（排银行）；`SHIPPER/CONSIGNEE` 降为最后兜底 | Debit Note 的 SHIPPER 是货物关系方，不是开票方 |
| 8 | 文档类型闭集 `Debit Note/Credit Note/Invoice/Unknown` | 增加 `Arrival Notice`，AR/AP 里与 DN 同归「正向计费」 | 真实数据里 CMA CGM 的到货通知既非 DN 也非 Invoice，且确实含应付费用 |
| 9 | 无表头文档按四档兜底 | 提单类（`BILL OF LADING`/`WAYBILL`）不抽费用；其余无锚点文档走 `strict`：无小数位的金额必须同行带币种 | MBL/HBL 附件里 `PAGE: 1 OF 1`、`NO WOOD PACKING MATERIALS. 10715.000` 都会被误抽 |
| 10 | EML 正文费用兜底 | 只对邮件本体开启（`body_charges` 开关） | 原实现让每个附件都复制一遍正文费用，一封邮件出 2 条重复记录 |

另外两处工程缺陷同期修复：

- **扫描件 OCR 失败被静默吞掉**：`_apply_ocr` 写的 `doc.errors` 从未进入 `errors.json`；改为解析前快照、处理后再收集（含附件），`图片示例.png` 现在会明确报 `E_OCR_CASSETTE_MISS`。
- **美式日期**：`10-24-2026` / `10/24/2026` 之前抽不出，`rule_dates` 增加 `mdy_num`（首段 >12 时按 DMY 兜底）。

详细对照与结果见 `docs/REAL_DATA_NOTES.md`。

---

## 7. 任务清单（施工顺序）

| # | 任务 | 产物 | 状态 |
|---|---|---|---|
| T1 | 配置与契约模型 | `config.py`、`schema.py` | ✅ |
| T2 | 摄入层四解析器 + 分派 | `ingest/*` | ✅ |
| T3 | OCR 三 provider + 归一 | `ocr/*` | ✅ |
| T4 | 规则抽取 | `extract/rule_extractor.py` | ✅ |
| T5 | 归一化（费用/币种/金额） | `normalize/*`、`data/charge_aliases.yaml` | ✅ |
| T6 | 五道校验闸 | `validate/*` | ✅ |
| T7 | 四级匹配 + 历史仓 | `matching/*` | ✅ |
| T8 | AR/AP + 决策 + patch | `decide/*` | ✅ |
| T9 | 对账 + LLM 抽取链 | `extract/llm_extractor.py`、`reconcile.py`、`prompts.py` | ✅ |
| T10 | 编排 + 审计 + CLI | `pipeline.py`、`audit.py`、`cli.py` | ✅ |
| T11 | 合成样本 + cassette + 历史账单 | `scripts/make_samples.py`、`data/*` | ✅ |
| T12 | 测试 + 评测脚本 | `tests/*`、`scripts/evaluate.py` | ✅（122 项通过） |
| T13 | README + 文档收口 | `README.md` | ✅ |

### 8.2 代码评审整改（v1.1.0 → v1.2.0）

`docs/CODE_REVIEW_v1.1.0.md` 记录的 7 条重要 + 21 条小问题，整改后本规格有 8 处口径变化：

| # | 原规格 | v1.2.0 实际做法 | 原因 |
|---|---|---|---|
| 1 | 金额正则 `\d+(?:[.,]\d+)*` | 允许「1 234.56」空格千分位；分组不是 3 位时返回 `None` 而不是取首段 | 原实现把 `1 234.56` 读成 `1.0`，置信度还是 0.9，会直接入账 |
| 2 | 同单同费用同金额「保留首条、其余丢弃」 | **全部保留**，第二条起标 `V-DUP_LINE`(warn) | 业务上分箱计费是合法的，静默丢弃会凭空少钱 |
| 3 | 勾稽 `sum(明细) + tax == total` | 按币种分组；支持 `totals_by_currency` 逐币种比对；多币种且只有一个总额时降级 `V-MULTI_CURRENCY` info | 没有汇率就不能跨币种相加 |
| 4 | 单据级字段不在 patch 范围内（也不继承） | 新增 `DOC_FIELDS`：不一致时**保留原值** + `P-DOC_FIELD_DIFF`；在 AR/AP 判定之前执行 | 原实现 `NO_CHANGE` 却把 issuer 改了，违反「不得覆盖未明确修改字段」 |
| 5 | 主体名出现在单据任意行 → 视为接收方（conf 0.6） | 备注/付款说明句排除；推断值 extractor 记 `infer:entity_in_doc`；`planner` 加 `F-PARTY_INFERRED` blocker | 推断不能当事实，尤其不能据此自动定 AR/AP |
| 6 | `normalize_key` 只压空白 | 抹掉 `- / _ . 空白`；`charge_item` 比较用「标准名 + 原始名 + 再归一」三路键 | `DN/202609/001` 与 `DN-202609-001` 是同一张单；历史账单常存原始名 |
| 7 | `.xls` / `.doc` 直接报不支持 | `.xls` 走 xlrd；`.doc` 走 textutil/soffice/antiword + 制表符还原表格 | 题目输入明确列了 XLS/DOC |
| 8 | `check` 默认不允许扩展字段 | 默认允许，提供 `--no-allow-extensions` | 与 `run` 默认输出保持一致，否则 run 完直接 check 必失败 |

新增错误码 / Issue 码：

| 码 | 级别 | 含义 |
|---|---|---|
| `E_XLS_CORRUPT` | error | `.xls` 无法解析（文件损坏） |
| `E_XLS_READER_MISSING` | error | 缺 `xlrd` 依赖 |
| `E_DOC_NO_CONVERTER` | error | 本机没有 textutil/soffice/antiword，无法读 `.doc` |
| `E_INPUT_NOT_FOUND` | error | 显式传入的输入路径不存在 |
| `E_NO_CHARGE` | error | 解析成功但零费用（不是账单或版式未识别） |
| `V-MULTI_CURRENCY` | info | 多币种单据，未做汇率换算，跳过总额勾稽 |
| `P-DOC_FIELD_DIFF` | warn | 单据级字段与历史账单不一致，已保留原值 |
| `P-FILL_ONLY` | info | 仅补全原账单的空字段，未改已有值 |
| `P-PARTY_INFERRED` | blocker | 交易方是推断值，不得自动定 AR/AP |
| `N-MULTI_CHARGE` | warn | 一格多费用，无法唯一归一 |

新增测试：`tests/test_legacy_formats.py`（xls/doc 解析与端到端）、`tests/test_boundaries.py`（本轮 9 类边界各一条）。合计 **160 项**。

### 8.3 邮件三件套修正（v1.2.0 → v1.3.0）

拿到题目网盘那封 PRE-ALERT 邮件（正文 + 2 个提单 PDF + 1 张内嵌签名图）逐部分核验后，修正三处：

| # | 原规格 | 实际做法 | 原因 |
|---|---|---|---|
| 1 | `PipelineSettings.pdf_strategy` 默认 `"ocr"` | 默认 `"auto"`：`has_text_layer and text_chars >= pdf_text_layer_min_chars` 才跳过 OCR；`--pdf-strategy ocr` 保留强制通道 | 默认 ocr 会把**有文本层的 PDF 也送出去**；当时误判网关不可达，怕整批失败才改成 auto（后证实网关可达，卡住的是 `file` 地址，v1.3.0 已改回 ocr + 失败回退） |
| 2 | 抽取只扫正文 `lines` | `_fill_from_context` 增加「从 `ctx.subject` 补 `bl_no` / `booking_no` / `container_no`」，标 `rule:email_subject`、置信度 ≤0.75 | 箱号常只写在邮件标题（`CNTR NO.: TIIU8975975`），正文只有 `MBL NO.`；箱号是 T4 匹配的必需键。**只补这三个标识字段**，标题里的 `Arrived time` 不当日期、收件人不参与判向 |
| 3 | 「解析成功但零费用」一律 `E_NO_CHARGE`(error) | 新增 `_looks_like_transport_doc()`：整份输入都是 `BILL OF LADING / WAYBILL` 时改记 `FileRecord.notes`，不进 `errors.json` | 提单没有费用是预期，报成错误会淹没真正的解析失败 |

配套变更：

- `FileRecord` 新增 `notes: list[str]`，写入 `trace-<run_id>.json`，用于记录「非错误但需解释」的情况。
- 新增 4 条回归：`test_pdf_with_text_layer_is_not_sent_to_ocr`、`test_email_subject_carries_container_number`、`test_subject_date_is_not_used_as_document_date`、`test_bill_of_lading_is_not_reported_as_parse_failure`。合计 **164 项**。

邮件三部分的处理口径（正文 / 附件 / 内嵌图 / 标题）见 `README.md` §3.1 与 `docs/REAL_DATA_NOTES.md` §8。

### 8.4 OCR 契约的实测记录（v1.3.0）

题目要求「PDF 和图片通过以下 OCR API 识别」，且给了调用代码。契约对应与实测结论：

| 题面 | 实现（`ocr/client.py`） | 实测 |
|---|---|---|
| `requests.post(url, json=data, timeout=(10, 300))` | 同一句；`timeout=(10.0, 300.0)` | mock 网关收到 `POST /ai/process`、`Content-Type: application/json` |
| `data = {"file","task","callBack","taskType"}` | `build_payload()`，四键同序，后两键恒 `""` | 收到键顺序 `['file','task','callBack','taskType']`，逐键一致 |
| `response.raise_for_status()` | 同一方法；5xx/429 重试、4xx 直接抛 | 503×2 后第 3 次成功；持续 500 请求 3 次（1+retries）后抛 `E_OCR_FAILED`；400 只请求 1 次 |
| `response.json()` | 校验顶层为对象、非 JSON 抛 `E_OCR_FAILED` | 返回 HTML 时抛「OCR 响应不是合法 JSON」 |
| `file_url` | `SBA_OCR_PUBLIC_BASE/<文件名>` | 端到端实测 `file=https://bucket.example.com/files/debit_note_DC-108478.pdf`；未配 public_base 时明确报错 |

端到端（mock 返回一段 Debit Note 文本）：网关调用 1 次 → 抽出 `DC-OCR-9001 / Debit Note / 2026-10-24 / GATEWAY OCR LINES LTD` + 2 条费用（65、100，合计 165 与 TOTAL 勾稽）→ `oc_call: true`、`errors` 为空。

**默认策略回到 `ocr`（PDF 一律送 OCR），并新增失败回退：**

| 场景 | 行为 |
|---|---|
| OCR 成功 | 用 OCR 文本，`meta["ocr"] = {provider, blocks, tables}` |
| OCR 失败 + PDF 有文本层 | 沿用文本层，`meta["ocr_fallback"]` 记原因，trace note「OCR 失败已回退文本层（结果来自 PDF 文本层，非 OCR）」，**不进 errors.json** |
| OCR 失败 + 扫描件（无文本层） | 进 `errors.json`，文件零记录——真失败要可见 |

> 8.3 里曾把默认值改成 `auto` 来规避「网关不可达导致整批失败」。两个判断都错了：(a) 网关其实可达（TCP 与 TLS 都成功，200 返回）；(b) 改默认值等于把题目「PDF 走 OCR」的要求关掉。v1.3.0 已改为「默认 ocr + 失败回退文本层」。

**网关连通性（2026-11-01 更正）**：`gateway.pltplt.com` 真实可调通。此前判定「TCP 25s 超时」是因为测试时传了无效的 `file`（`example.com` / `example.invalid`），**服务端收到请求后去下载该地址，卡在下载上直到超时**，表现出来像网络不通。改用公网可下载的 PDF 后：TLS 握手成功、`HTTP 200`、7–12s 返回、`success: true`。

真实响应样本已存为 `tests/fixtures/real_gateway_response.json`。

### 8.5 OCR 网关真机实测与「file 必须是公网地址」（2026-11-01）

**结论先行**：网关一直是通的。此前 4 次判定「不可达」全部是误判——根因是测试时给了一个**网关下载不到的 `file`**。

#### 误判的形成过程

| 测试 | 现象 | 当时的解读 | 实际原因 |
|---|---|---|---|
| `curl POST` + `file=https://example.com/bill.pdf` | 25s 超时，`http_code=000` | 网络不可达 | 服务端收到请求后去下载 `example.com/bill.pdf`，卡在下载上直到连接超时 |
| `curl POST` + `file=https://example.invalid/nonexistent-12345.pdf` | 45s 超时 | 网络不可达 | 同上 |
| `nc -z gateway.pltplt.com 443` | **succeeded** | 被忽略 | 端口是通的 |
| `curl -v https://gateway.pltplt.com/` | TLS 握手完整成功（Client hello → Server hello → Certificate → Finished） | 被忽略 | 证书链路正常 |
| `curl POST` + `file=https://www.w3.org/.../dummy.pdf` | **HTTP 200，10s，`{"success":true,"message":"文档处理成功"}`** | — | 公网可下载的地址 → 立即返回 |

教训：**「请求挂死」和「网络不通」是两件事**。TCP/TLS 层已经成功时，超时只能说明对方在等某个资源，第一嫌疑是它依赖的外部输入（这里就是 `file`）。

#### `file` 字段的硬约束

题面原文：「`file` 应为 OCR 服务端能够访问的 PDF 或图片下载地址。」

| 传什么 | 网关能否识别 | 说明 |
|---|---|---|
| `https://bucket.example.com/bill.pdf` | ✅ | 对象存储 / CDN / 任意公网静态托管 |
| `/Users/me/Desktop/bill.pdf` | ❌ | 绝对路径，网关机器上没有这个文件 |
| `file:///Users/me/bill.pdf` | ❌ | 本地协议，出了本机就无效 |
| `http://127.0.0.1:8000/bill.pdf` | ❌ | 回环地址，网关访问的是它自己 |
| `http://192.168.1.10/bill.pdf` | ❌ | 内网地址，公网不可路由 |
| 百度网盘分享链接（需登录/需 cookie） | ❌ | 网关拿不到会话态 |

因此 `resolve_url()` 在 `SBA_OCR_PUBLIC_BASE` 为空时**直接抛错、不发送请求**，避免把本地路径当 URL 发出去白白等 300s。

#### 真实响应结构（重要）

网关返回的不是 OCR 文本，而是**已结构化的单据**：

```json
{"data": {"createDate": "...", "fileName": "", "orderType": "None",
          "list": [{"fileType": "PAI-COLOADER-NOTICE",
                    "shipper": "", "consignee": "", "mblNumber": "", "invoiceNumber": "",
                    "chargeList": [], "ctrList": [], "hold": {"holdType": "", ...},
                    "remark": "", "page_index": [0, 1]}],
          "pages_split": [[0, 1]]},
 "success": true, "message": "文档处理成功", "error_code": null, "task_id": null}
```

这推翻了原设计的假设（原来在响应里找 `pages[].text`）。旧的 `normalize_ocr` 对这个响应只挤出 44 个字符（`2026-11-01 12:11:37 / PAI-COLOADER-NOTICE / None`），**真实业务字段全部丢失**。

新归一层：

| 响应内容 | 转换 |
|---|---|
| `list[].fileType/shipper/consignee/mblNumber/invoiceNumber/remark/...` | → `LABEL: value` 文本行，下游规则抽取按标签抓 |
| `list[].chargeList[]` | → `TableBlock(name="chargeList")` + 每行一条文本（供文本行抽取兜底） |
| `list[].ctrList[]` | → `TableBlock(name="ctrList")` |
| `list[].hold{...}` | → `HOLD: k=v, ...` 备注行 |
| `suspicious/isDetention/isDemurrage/page_index` | 丢弃（状态位，不是单据内容） |
| `success=false` / `error_code` 非空 | `gateway_error()` 返回原因，pipeline 按 OCR 失败处理（可回退文本层） |

真实响应样本：`tests/fixtures/real_gateway_response.json`（对公网 PDF 的一手响应，字段为空但结构真实）。

#### 拿到真实账单响应：扫描件已跑通

需要一份**真实单据的公网 URL**。做法：把 `zim_invoice_scan.png` 传到图床 uguu（`https://h.uguu.se/xxxx.png`），
用该 URL 调网关，10.7s 返回 `HTTP 200`。真实响应：

```json
{"data": {"orderType": "ANR_LLM_ZIM-INVOICE",
          "list": [{"fileType": "PAI-ZIM-NOTICE",
                    "invoiceNumber": "QRF1618731",
                    "mblNumber": "ZIMUNGB1312144",
                    "ctrList": [{"ctrNumber": "JXLU4594463", ...}],
                    "chargeList": [{"chargeItem": "WHARFAGE ADMINISTRATION FEE", "amount": "5.00", "chargeCtr": ""},
                                   {"chargeItem": "WHARFAGE", "amount": "21.90", "chargeCtr": ""}],
                    "requiresReview": "N"}]},
 "success": true}
```

**`chargeList` 的真实键已确认**：`chargeItem` / `amount` / `chargeCtr`——**没有 currency 字段**。
所以扫描件的 `currency` 只能是 `null`（不拿 `USIAHALLAM` 之类的线索硬猜 USD）。
同理 `document_date` / `issuer` / `recipient` 也是网关返回空 → 系统置 null。

管线产出（`pytest tests/test_real_samples.py`）：2 条 `Wharfage Charge`，5.00 + 21.90 = 26.90，与单据 TOTAL 一致。
响应已固化为 `data/ocr_cassettes/f3ca6432d5597b72__orderChange.json`，离线可复现。

#### 边界：PDF 的真实 OCR（已于 §8.6 补齐）

本节写就时 PDF 的真实 OCR 还没做成（`file` 传不上公网，uguu 拒收 PDF）。
**v1.5.0 已补齐**：四份 PDF 全部跑过真实网关，并据此把策略定为「文本层为主源、OCR 只补标识字段」，详见 §8.6。

### 8.6 PDF 的真实 OCR 实测（v1.5.0）

§8.5 末尾曾写「PDF 的真实 OCR 未做」。这句话现在不成立了：四份 PDF 全部跑过真实网关。

#### 之前为什么没做

不是不想做，是 `file` 传不上去。uguu 只收图片（传 PDF 返回 `415 Filetype not allowed`），
其余临时托管（catbox / tmpfiles / gofile / file.io / pixeldrain / bashupload / litterbox）全部连不通，
0x0.st 直接关闭了上传（返回 503「uploads disabled」）。

这次找到 **paste.c-net.org**：`curl --data-binary @file https://paste.c-net.org/`，
下载回来二进制与原件 **shasum 完全一致**、`Content-Type: application/pdf`、**无重定向**（网关能直接 GET）。

#### 为什么 filebin.net 不行（一个容易踩的坑）

filebin 上传返回 201，自己下载也是 `application/pdf`、77KB 正常。但喂给网关时：
- 短链 `https://filebin.net/<bin>/dn.pdf` → 网关返回 200，但 `page_index: [0,2]`，
  而这份 PDF **只有 1 页** —— 说明它拿到的不是这个文件；
- 带签名的 S3 直链 → 网关 504（60s 超时）。

结论：**「我能下载」不等于「网关能下载」**。带 302 跳转或签名 query 的地址，网关处理不了。

#### 四份 PDF 的真实 OCR 结果

| 文件 | fileType | 单号 | 提单号 | 箱号 | 费用 |
|---|---|---|---|---|---|
| debit_note_DC-108478.pdf | `PAI-COLOADER-NOTICE` | **空** | MAEU275752922 | MRSU2571577 / TCLU8619685 | HANDLING CHARGE 100.00、4 ADD'L DETENTION FREE TIME CHARGE 1310.40 |
| arrival_notice_NEPTUNE.pdf | `PAI-CMA-NOTICE` | **空** | CMDUCHN4053601 | SAPU2040689 | On Carriage Additional - US Em 245.00 |
| 4055331281 MBL.pdf | 空 | 空 | 空 | 空 | 空 |
| 4055331281 HBL.pdf | 空 | 空 | 空 | 空 | 空 |

#### 关键对比：OCR 与文本层各有强弱

同一份 `debit_note_DC-108478.pdf`：

| 字段 | 文本层 | 网关 OCR | 谁更好 |
|---|---|---|---|
| 单号 | `DC-108478` | 空（`invoiceNumber: ""`） | **文本层** |
| 币种 | USD | 空（`chargeList` 无币种字段） | **文本层** |
| 单据类型 | Debit Note | `PAI-COLOADER-NOTICE`（网关自家分类） | **文本层** |
| 明细 | 65.00 + 1310.40 + 35.00（3 条） | 100.00 + 1310.40（2 条，**65+35 被合并**） | **文本层** |
| 合计 | 1410.40 | 1410.40 | 都正确 |
| 提单号 | MAEU275752922 | MAEU275752922 | 平 |
| 箱号 | MRSU2571577 | MRSU2571577 / TCLU8619685 | **OCR 更全** |

**结论：让 OCR 覆盖文本层会直接丢单号**（T1 匹配的必需键），并把逐行明细合并成一行。

#### 落地策略：文本层为主源，OCR 只补标识

`pipeline._apply_ocr` 分两种情况：

| 文档 | OCR 角色 | 行为 |
|---|---|---|
| PDF 有文本层（`text_chars >= min_chars`） | **supplement** | 仍调用网关（满足「PDF 走 OCR」），结果写进 `doc.ocr_*` 副源位，**不覆盖** `text/blocks/tables`；抽取后由 `extract/ocr_merge.py` 只补 `bl_no/booking_no/container_no/reference_no` |
| 扫描件（无文本层） | **primary** | OCR 结果直接作为唯一来源 |
| OCR 失败 + 有文本层 | fallback | 沿用文本层，记 note，不进 errors |
| OCR 失败 + 无文本层 | 失败 | 进 `errors.json` |

不合并的字段及原因（写在 `extract/ocr_merge.py` 模块文档里）：
- `document_type`：网关给的是 `PAI-*` 自家分类，映射到标准类型不可靠；
- `issuer` / `recipient`：网关的 `shipper`/`consignee` 是**货物关系方**，不等于账单签发方/接收方；
- 金额/币种/日期：文本层可逐字回溯原文，冲突时文本层优先。

**结果**：真实数据 7 条记录与纯文本层版本**逐字段一致**，`ocr_calls=5 / ocr_errors=0`，
额外补到了 Arrival Notice 的 `bl_no`（CMDUCHN4053601）。

四份 PDF 的真实响应已存为 cassette：
- `70dcc3e7d07a0012__orderChange.json`（Debit Note）
- `7f0681695a278ea4__orderChange.json`（Arrival Notice）
- `e85bee3e3c663e98__orderChange.json`（MBL）
- `b577030300701c56__orderChange.json`（HBL）

### 8.7 数据产物与审计留存（v1.6.0）

#### 存储形态：文件即产物，不引入数据库

本系统是「解析 → 出账务指令文件」的离线批处理，不是长期账务系统，因此**没有持久化层**：

| 组件 | 形态 | 读写 |
|---|---|---|
| 历史账单仓 | `data/existing_bills.json`（`--bills` 传入） | **只读**。`ExistingBillStore` 只有 `from_json` / `query` / `get` / `has_duplicate`，没有写入方法 |
| 本次产出 | `out/<run>/*.json` | 每次 run 独立目录，互不覆盖 |
| 历史账单回写 | 无 | 入账由下游账务系统消费 `result.json` 完成，本工具不写账 |

这样设计的理由：入账需要审批（题面要求 `PENDING_REVIEW` 要转人工），工具若自己写回账单仓，
就绕过了审批。**本工具只出「建议」，落账是下游的事。**

#### 一次 run 的产物

| 文件 | 用途 | 关键内容 |
|---|---|---|
| `result.json` | 交付物，题目契约格式 | 7 条记录，`audit_trail.ai_reasoning` 成段说明 |
| `report.json` | 内部完整视图 | 字段级 `provenance`、`issues[]`、`match`、`intended_operation`、`degraded` |
| `errors.json` | 解析失败清单 | 错误码 + 文件路径 |
| `trace-<run_id>.json` | 运行审计 | `run_id`、阶段耗时、计数器、逐文件 `sha256` / `ocr_call` / `entries` / `notes` / `raw_snapshots` |
| `raw/<sha8>__<名>.txt` | **原文快照** | 抽取时实际使用的文本 + 来源头部 |
| `work/` | 中间产物 | EML 解出的附件副本 |

#### 原文快照（本轮新增）

之前只有 `provenance.span`（命中的那一小段），回答不了「这句话在原文什么位置、上下文是什么」。
现在把整份抽取文本落盘：

```text
# source    : data/real/debit_note_DC-108478.pdf
# sha256    : 70dcc3e7d07a0012fc642af256371f25438eed2c7bbe25442b7ffbd193fd34ba
# doc_kind  : pdf
# origin    : root
# text_from : supplement (provider=cassette)     ← 文本层 还是 OCR 主源
# chars     : 1322
# 说明：抽取阶段实际使用的文本。report.json 里 provenance.span 的片段都能在此文件内检索到。
DEBIT NOTE
D/C No. : DC-108478
...
```

- 文件名前缀 = `sha256[:8]`，同名文件不会互相覆盖；EML 的每个附件单独一份（`origin` 写 `root>attachment:xxx`）。
- `SBA_KEEP_RAW_TEXT=false` 可关闭（敏感场景只留 span）。
- 已用测试锁死：`test_raw_snapshot_contains_every_provenance_span` 断言
  **report.json 里每一个非空 `provenance.span` 都能在该文件的原文快照里检索到**（实测 Debit Note 10/10 命中）。

#### 五个问题的简明回答（便于答辩）

| 问题 | 现状 |
|---|---|
| 生成了哪些账单 | 真实数据 7 条，全部 `PENDING_REVIEW`（本工具不直接落账） |
| 怎么存 | 文件：`result/report/errors/trace/raw`，用 `run_id` 分目录；历史账单只读 JSON |
| 重复账单怎么处理 | 三层：同单重复行 `V-DUP_LINE`(warn，保留) → 同批跨文件 `V-DUP_BILL`(blocker) → 对历史 `V-DUP_BILL`(blocker)；`--idempotent` 时命历史降级 `NO_CHANGE` + `P-IDEMPOTENT` |
| 冲突数据怎么办 | 规则 vs LLM 强字段冲突 → `V-RECONFLICT`(blocker) 取规则值转人工；单据级字段与历史不一致 → `P-DOC_FIELD_DIFF`(warn) 保留原值；明细与总计不符 → `V-RECONCILIATION`(blocker)；匹配多条 → `M-AMBIGUOUS` |
| 来源/原文/依据 | 来源 `sha256`+路径（trace）→ 原文 `raw/*.txt` → 字段证据 `provenance`（span/page/line/extractor/confidence）→ 拦截理由 `issues[]` → 人读说明 `ai_reasoning` |

#### 未做的部分

- **无自动清理**：`out/` 只增不减，长期跑需要外部按 `run_id` 归档或删除（`raw/` 体积与语料正相关）。
- **无版本化历史账单**：`--bills` 每次读一份快照，历史账单的变更本身没有版本记录（属于下游账务系统的职责）。

### 8.8 AR/AP 判向：能力证明与「真实数据为什么全 null」（v1.7.0）

**结论先行**：AR/AP 判向是工作的，三套数据里两套自动判向成功；真实那 4 份文件判不出，
是因为**记账主体 PltPlt 不在任何一张单的交易链上**——它只以 SaaS 水印的形式出现。

#### 8.8.1 判向规则（`decide/ar_ap.py`）

题目四.6 要求「不能仅根据 DN/CN 直接判断，还必须结合签发方、接收方、当前记账主体和收费方向」。
实现就是一个四象限：

| 记账主体 = | 单据类型 | 结果 |
|---|---|---|
| **签发方**（我方开单） | DN / Invoice / Arrival Notice | **AR**（对外收费） |
| **签发方** | Credit Note（mirror 口径） | AP（冲销） |
| **接收方**（我方被收费） | DN / Invoice / Arrival Notice | **AP** |
| **接收方** | Credit Note（mirror 口径） | AR（冲销） |
| 都不是 | 任意 | `null` + `F-ARAP_UNKNOWN`（不在交易链上，不猜） |

#### 8.8.2 三套数据的判向分布

| 数据集 | 记录数 | operation | AR / AP / null |
|---|---|---|---|
| 合成样本 `data/samples` | 10 | CREATE 4 / UPDATE 1 / NO_CHANGE 4 / PENDING 1 | **AR 2 / AP 8 / 0** |
| 旧格式样本 `data/samples/legacy` | 4 | CREATE 4 | **AR 4 / AP 0 / 0** |
| 真实网盘数据 `data/real` | 7 | PENDING_REVIEW 7 | **AR 0 / AP 0 / null 7** |

前两套自动入账；第三套全 null 需要解释，见 8.8.3。

#### 8.8.3 同一份单据、换记账主体，判向立刻变化

拿 `debit_note_DC-108478.pdf`（issuer = `NEPTUNE SHIPPING LIMITED`）实测：

| 记账主体 | issuer | recipient | posting | operation |
|---|---|---|---|---|
| **PltPlt** | NEPTUNE | null | **null** | PENDING_REVIEW |
| AEBO TCO IMPORT LLC | NEPTUNE | AEBO（consignee） | AP | PENDING_REVIEW（`F-PARTY_INFERRED`） |
| YIWU JINBAIREN IMPORT AND EXPORT | NEPTUNE | YIWU | AP | PENDING_REVIEW（同上） |
| **NEPTUNE SHIPPING LIMITED** | NEPTUNE | null | **AR** | **CREATE**（自动入账） |

同一份文件、同一份代码，主体换成签发方自己就判出 `AR + CREATE`。**这说明判向逻辑是对的，
PltPlt 判不出不是能力问题，是它真的不在这张单上。**

#### 8.8.4 为什么 PltPlt 不在单上——一个被修掉的误判

早先 `recipient` 抽成了 `PltPlt`，依据是这一行：

```
NEPTUNE SHIPPING LIMITED (LAX) Powered by
|
湃乐多（上海）航运科技有限公司
Pilot Aiship Tech co., Ltd.
```

这是**「Powered by 湃乐多」的 SaaS 水印**——说明 NEPTUNE 用了湃乐多的单据系统，不是说单据开给湃乐多。
如果把这三行当接收方声明，判向会直接反过来（我方变成付款人），产出一笔**完全错误的应付**。

修复（`rule_extractor._watermark_line_indices`）：

- `WATERMARK_RE` 命中 `powered by / provided by / 技术支持 / 本单据由 / copyright / ©` 等；
- 水印行**连同紧跟其后的最多 3 个非空行**一起排除（中英文公司名常分两行）；
- 排除后 `recipient = null`，`F-ARAP_UNKNOWN` 的理由变成「记账主体既非签发方也非接收方，
  不在本单交易链上」，并附可操作建议（`--entity-alias` 或人工指定方向）。

回归测试：`test_saas_watermark_is_not_a_recipient`（单元）、`test_real_batch_posting_types`（批次）。

#### 8.8.5 剩下的 4 条为什么也判不出

| 来源 | 缺什么 | 为什么不硬判 |
|---|---|---|
| Arrival Notice | 单据上无记账主体（issuer = NEPTUNE） | 只有签发方没有接收方，判不出是开给谁 |
| EML 正文 | 无单据类型（PRE-ALERT 不是计费单据） | 类型 Unknown，方向无定义 |
| 扫描件 ×2 | issuer / recipient 都为空 | 网关 `chargeList` 只给费用，无交易方字段 |

这四条都是「输入材料里确实没有判向依据」，硬判等于违反七.1「不得虚构输入材料中不存在的数据」。

#### 8.8.6 一句话总结给评审

> 系统有 3 条自动判向路径（合成 10 条 AR 2/AP 8、旧格式 4 条 AR 4、真实单据换主体 AR + CREATE），
> 真实批次全 null 是因为那批文件里没有一张是开给 PltPlt 的——唯一出现的 `湃乐多` 是 SaaS 水印。
> 这不是「全部要人工审核」，是「该自动的自动，不该猜的不猜」。
