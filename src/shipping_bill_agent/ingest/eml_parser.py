"""EML 解析：邮件头 + 正文（text/plain 优先，html 降级）+ 附件递归（含嵌套邮件）。"""

from __future__ import annotations

import email
from email import policy
from email.message import Message
from pathlib import Path
from urllib.parse import unquote

from ..config import Settings
from ..schema import Block, RawDocument, SourceRef
from ..utils import clean_text, html_to_text, preserve_lines, safe_filename, sha256_bytes, sha256_file

MAX_DEPTH = 5


def _decode_header_value(msg: Message, name: str) -> str | None:
    raw = msg.get(name)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _collect_body(msg: Message) -> tuple[str, str]:
    """返回 (正文文本, 来源类型 plain|html)。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_content()
        except (LookupError, ValueError):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(payload, str):
            continue
        if ctype == "text/plain":
            plain_parts.append(payload)
        elif ctype == "text/html":
            html_parts.append(payload)
    if plain_parts:
        return "\n".join(plain_parts), "plain"
    if html_parts:
        return html_to_text("\n".join(html_parts)), "html"
    return "", "none"


def _iter_attachment_parts(msg: Message):
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename:
            yield part, filename
        elif part.get_content_type() == "message/rfc822":
            nested = part.get_payload()
            if isinstance(nested, list) and nested:
                yield part, "nested.eml"


def _is_inline_body_image(part: Message) -> bool:
    """正文内嵌图片（签名/Logo，带 Content-ID 且 inline）：不是业务附件，不送 OCR。"""
    if not part.get_content_type().startswith("image/"):
        return False
    if part.get_content_disposition() == "attachment":
        return False
    return bool(part.get("Content-ID")) or part.get_content_disposition() == "inline"


def _attachment_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if part.get_content_type() == "message/rfc822":
        nested = part.get_payload()
        if isinstance(nested, list) and nested:
            return nested[0].as_bytes()
        if isinstance(nested, Message):
            return nested.as_bytes()
    raw = part.get_payload()
    return raw.encode("utf-8", errors="replace") if isinstance(raw, str) else b""


def parse_eml(
    path: Path,
    *,
    workdir: Path,
    settings: Settings,
    origin: str = "root",
    depth: int = 0,
) -> RawDocument:
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)

    subject = _decode_header_value(msg, "Subject")
    sender = _decode_header_value(msg, "From")
    recipient = _decode_header_value(msg, "To")
    cc = _decode_header_value(msg, "Cc")
    date = _decode_header_value(msg, "Date")

    body_raw, body_kind = _collect_body(msg)
    body_text = clean_text(body_raw)

    # 块级文本保留行内空白，供「按列切分」使用；正文是否成单由 pipeline 判定
    lines = preserve_lines(body_raw)
    text = clean_text(
        "\n".join(
            [
                f"Subject: {subject}" if subject else "",
                f"From: {sender}" if sender else "",
                f"To: {recipient}" if recipient else "",
                f"Date: {date}" if date else "",
                "",
                body_text,
            ]
        )
    )
    blocks = [Block(kind="paragraph", text=line, line_no=i + 1) for i, line in enumerate(lines)]

    source = SourceRef(
        path=str(path),
        sha256=sha256_file(path),
        kind="eml",
        origin=origin,
        size_bytes=len(raw),
    )

    doc = RawDocument(
        source=source,
        doc_kind="eml",
        text=text,
        blocks=blocks,
        meta={
            "subject": subject,
            "from": sender,
            "to": recipient,
            "cc": cc,
            "date": date,
            "body_kind": body_kind,
            "attachments": [],
        },
    )

    if depth >= MAX_DEPTH:
        doc.errors.append("E_PARSE_FAILED: EML 嵌套层级超限，停止递归")
        return doc

    attach_dir = workdir / "attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)

    for part, filename in _iter_attachment_parts(msg):
        clean_name = safe_filename(unquote(filename))
        if _is_inline_body_image(part):
            doc.meta.setdefault("inline_images", []).append(clean_name)
            continue
        data = _attachment_bytes(part)
        if not data:
            continue
        digest = sha256_bytes(data)[:8]
        target = attach_dir / f"{digest}_{clean_name}"
        try:
            target.write_bytes(data)
        except OSError as exc:
            doc.errors.append(f"E_PARSE_FAILED: 附件落盘失败 {filename} ({exc})")
            continue

        doc.meta["attachments"].append(clean_name)

        from .dispatch import ingest  # 延迟导入避免循环依赖

        child = ingest(
            target,
            workdir=workdir,
            settings=settings,
            origin=f"{origin}>attachment:{clean_name}",
            depth=depth + 1,
        )
        doc.children.append(child)

    return doc
