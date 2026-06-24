"""
Convert uploaded / pasted documents into clean markdown for the wiki pipeline.

Supported inputs: .md / .markdown / .txt (text), .pdf (pdfplumber), .docx
(python-docx). Heavy parsers are imported lazily inside each branch so this
module imports fine even where those libs aren't installed; they ARE in
requirements.txt for the real app / PyInstaller build.

Every input becomes a markdown string with a `# Title` heading (so Stage-1
distillation and citations have a real title), and is saved by the caller as a
`<name>.md` under scraped_data/{category}/ — the "category" is the wiki domain
the user assigns, which later drives per-category KB construction.
"""

import io
import os
import re

# Extensions the import UI/endpoint accepts.
TEXT_EXTS = {".md", ".markdown", ".txt"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
SUPPORTED_EXTS = TEXT_EXTS | PDF_EXTS | DOCX_EXTS


def supported_accept_attr() -> str:
    """Value for an <input type=file accept="..."> covering all supported types."""
    return ",".join(sorted(SUPPORTED_EXTS))


def _decode_text(data: bytes) -> str:
    """Decode text bytes trying UTF-8 → GBK (common for Chinese) → latin-1."""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def _title_from_filename(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r'[_\-]+', ' ', stem).strip()
    return stem or "未命名文档"


def _wrap_markdown(title: str, body: str) -> str:
    body = (body or "").strip()
    return f"# {title}\n\n{body}\n"


def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as e:
        raise ValueError("服务器未安装 PDF 解析库（pdfplumber）") from e
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(txt.strip())
    return "\n\n".join(pages)


def _extract_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise ValueError("服务器未安装 Word 解析库（python-docx）") from e
    document = docx.Document(io.BytesIO(data))
    parts = []
    for para in document.paragraphs:
        t = (para.text or "").strip()
        if t:
            # Keep heading styles as markdown headings for better structure.
            style = (para.style.name or "").lower() if para.style else ""
            if style.startswith("heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "2"
                parts.append("#" * min(6, int(level) + 1) + " " + t)
            else:
                parts.append(t)
    # Also pull text from tables (often holds the real content in gov/enterprise docs).
    for table in getattr(document, "tables", []):
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def extract_to_markdown(filename: str, data: bytes) -> str:
    """Convert raw upload bytes to a markdown string with a title heading.

    Raises ValueError on an unsupported extension, a missing parser library, an
    empty document, or a parse failure (caller surfaces the message to the user).
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".doc":
        raise ValueError("不支持旧版 .doc（二进制），请另存为 .docx 或 PDF 后再上传")
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件类型：{ext or '（无扩展名）'}")

    title = _title_from_filename(filename)
    if ext in TEXT_EXTS:
        text = _decode_text(data)
        # An .md that already starts with a heading is passed through unchanged.
        if ext in (".md", ".markdown") and text.lstrip().startswith("#"):
            return text if text.endswith("\n") else text + "\n"
        body = text
    elif ext in PDF_EXTS:
        body = _extract_pdf(data)
    elif ext in DOCX_EXTS:
        body = _extract_docx(data)
    else:                                   # unreachable, guarded above
        raise ValueError(f"不支持的文件类型：{ext}")

    if not body or not body.strip():
        raise ValueError("未能从文件中提取到文本内容（可能是扫描件/纯图片）")
    return _wrap_markdown(title, body)


def markdown_from_text(text: str, title: str = "") -> str:
    """Wrap pasted plain text / markdown into a titled markdown document."""
    text = (text or "").strip()
    if not text:
        raise ValueError("粘贴内容为空")
    title = (title or "").strip()
    # If the pasted text is already markdown with a leading heading, keep it.
    if not title and text.lstrip().startswith("# "):
        return text if text.endswith("\n") else text + "\n"
    return _wrap_markdown(title or "粘贴内容", text)


def safe_md_name(filename: str, fallback: str = "doc") -> str:
    """A safe, unique-ish `<stem>.md` filename derived from an upload name."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    stem = re.sub(r'[^0-9A-Za-z一-鿿_\-]+', '_', stem).strip('_')
    return (stem or fallback)[:60] + ".md"
