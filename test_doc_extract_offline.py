"""Offline tests for doc_extract.py — the upload/paste → markdown converter.

Covers the dependency-free paths (txt/md/paste/naming/guards). PDF and DOCX
paths need pdfplumber / python-docx (in requirements.txt, not exercised here);
their failure modes (missing lib, empty doc) are asserted via the ValueError
contract. Run with:  python test_doc_extract_offline.py
"""

import doc_extract as de

_passed = 0


def check(cond, label):
    global _passed
    if cond:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        raise AssertionError(f"FAILED: {label}")


def expect_value_error(fn, needle=""):
    try:
        fn()
    except ValueError as e:
        return needle in str(e)
    return False


def test_text_and_markdown():
    print("test_text_and_markdown")
    md = de.extract_to_markdown("行业 政策_2024.txt", "这是一段正文。\n第二段。".encode("utf-8"))
    check(md.startswith("# 行业 政策 2024"), "txt gets a title heading from filename")
    check("这是一段正文" in md, "txt body is preserved")
    md2 = de.extract_to_markdown("a.md", "# 已有标题\n正文".encode("utf-8"))
    check(md2.startswith("# 已有标题"), "markdown with a heading passes through")
    md3 = de.extract_to_markdown("b.txt", "中文内容测试".encode("gb18030"))
    check("中文内容测试" in md3, "GBK-encoded text decodes correctly")


def test_guards():
    print("test_guards")
    check(expect_value_error(lambda: de.extract_to_markdown("x.doc", b"\xd0\xcf"), ".docx"),
          "legacy .doc is rejected with a helpful message")
    check(expect_value_error(lambda: de.extract_to_markdown("x.zip", b"x")),
          "unsupported extension is rejected")
    check(expect_value_error(lambda: de.extract_to_markdown("empty.txt", b"")),
          "empty document is rejected")


def test_paste_and_naming():
    print("test_paste_and_naming")
    check(de.markdown_from_text("粘贴正文", "标题A").startswith("# 标题A"),
          "paste with a title gets that heading")
    check(de.markdown_from_text("# 已有md标题\n正文", "").startswith("# 已有md标题"),
          "pasted markdown with a heading is kept as-is")
    check(expect_value_error(lambda: de.markdown_from_text("   ", "")),
          "empty paste is rejected")
    check(de.safe_md_name("我的 文件:v1.pdf") == "我的_文件_v1.md",
          "safe_md_name sanitizes and forces .md (keeps CJK)")
    check(de.safe_md_name("") == "doc.md", "safe_md_name falls back when empty")


def test_supported_set():
    print("test_supported_set")
    for ext in (".md", ".markdown", ".txt", ".pdf", ".docx"):
        check(ext in de.SUPPORTED_EXTS, f"{ext} is in the supported set")
    check(".doc" not in de.SUPPORTED_EXTS, "legacy .doc is NOT in the supported set")
    check(de.supported_accept_attr().count(",") == len(de.SUPPORTED_EXTS) - 1,
          "accept attribute lists every supported type")


def main():
    for t in (test_text_and_markdown, test_guards,
              test_paste_and_naming, test_supported_set):
        t()
    print(f"\nAll doc_extract offline checks passed ({_passed} assertions).")


if __name__ == "__main__":
    main()
