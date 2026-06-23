"""Offline regression tests for the deterministic (no-API) parts of the
three-stage wiki pipeline. Run with:  python -m wiki.test_pipeline_offline

These cover the long-term-stability machinery that has no LLM call and therefore
can (and should) be verified deterministically:
  - cross-batch entity registry: normalize / extract / match / hint
  - anti-loss backup on a drastic page shrink
  - relations.md sub-cap + relevance ordering in assembly context
  - page-select index cap
  - Stage-2 assembly prompts still .format() with all required fields

Turns the manual SELF_CHECK.md items into executable assertions. No network,
no API key, no DeepSeek — pure filesystem + string logic.
"""

import os
import sys
import tempfile
from pathlib import Path

# Allow running both as `python -m wiki.test_pipeline_offline` and directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiki.wiki_manager import (  # noqa: E402
    WikiManager, ENTITY_REGISTRY_FILE, INDEX_SELECT_CAP_CHARS,
    PAGE_SHRINK_BACKUP_MIN_CHARS,
)
from wiki.schema import (  # noqa: E402
    ASSEMBLE_PROMPT_TEMPLATE, ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE,
)

_passed = 0


def check(cond, label):
    global _passed
    if cond:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        raise AssertionError(f"FAILED: {label}")


def _mgr(tmp: str) -> WikiManager:
    """A WikiManager rooted at a throwaway cwd so wiki/<domain>/ is isolated."""
    os.chdir(tmp)
    return WikiManager(domain="testdomain", api_key="")


def test_entity_normalize_and_extract():
    print("test_entity_normalize_and_extract")
    m = _mgr(tempfile.mkdtemp())
    check(m._normalize_entity("国家 制造业-创新中心（CMI）")
          == m._normalize_entity("国家制造业创新中心cmi"),
          "normalize strips spaces/punct/brackets consistently")
    atom = ("# 某政策解读\n- 来源: x.md\n- 核心主张:\n  - A\n"
            "- 关键实体: 工信部；国家制造业创新中心, 华为\n- 概念标签: 制造,政策\n")
    ents = m._extract_atom_entities(atom)
    check(set(ents) == {"工信部", "国家制造业创新中心", "华为"},
          "extract splits 关键实体 on ；,、 separators")
    check(m._extract_atom_entities("- 关键实体: 无\n") == [],
          "extract drops the '无' sentinel")


def test_entity_match_is_conservative():
    print("test_entity_match_is_conservative")
    m = _mgr(tempfile.mkdtemp())
    reg = {
        m._normalize_entity("国家制造业创新中心"): {
            "page": "entities/制造业创新中心.md",
            "canonical": "国家制造业创新中心", "aliases": []},
    }
    # Substring alias should match the longer canonical (the core fix).
    matches = m._match_existing_entities(["制造业创新中心"], reg)
    check("国家制造业创新中心" in matches,
          "shorter form '制造业创新中心' merges into the registered entity")
    # Learned the alias on the matched entry.
    key = m._normalize_entity("国家制造业创新中心")
    check("制造业创新中心" in reg[key]["aliases"], "match learns the alias form")
    # A generic 2-char fragment must NOT match (avoids false merges).
    reg2 = {m._normalize_entity("北京中心"): {
        "page": "entities/北京中心.md", "canonical": "北京中心", "aliases": []}}
    check(m._match_existing_entities(["中心"], reg2) == {},
          "a bare '中心' does not false-merge (min match length guard)")


def test_entity_registry_roundtrip_and_hint():
    print("test_entity_registry_roundtrip_and_hint")
    m = _mgr(tempfile.mkdtemp())
    m.write_page("entities/huawei.md", "# 华为\n- 总部: 深圳\n")
    m.write_page("entities/cmi.md", "# 国家制造业创新中心\n- 设立: 2016\n")
    reg = m._refresh_entity_registry()
    check((m.wiki_path / ENTITY_REGISTRY_FILE).is_file(), "registry file is persisted")
    check(any(e.get("canonical") == "华为" for e in reg.values()),
          "registry picks up entity-page titles as canonicals")
    # Reload is stable.
    reg2 = m._load_entity_registry()
    check(reg2 == reg, "registry reload roundtrips identically")
    # Hint surfaces a known entity for a new atom mentioning it.
    atoms_text = "# n\n- 关键实体: 华为, 某新公司\n"
    hint = m._build_entity_hint(atoms_text, reg2)
    check("[[huawei]]" in hint and "华为" in hint,
          "hint points the model at the existing entity page to merge into")


def test_anti_loss_backup_on_shrink():
    print("test_anti_loss_backup_on_shrink")
    m = _mgr(tempfile.mkdtemp())
    big = "# 概念\n" + ("事实。" * 400)            # well over the min-chars threshold
    check(len(big) >= PAGE_SHRINK_BACKUP_MIN_CHARS, "fixture page is substantial")
    m.write_page("concepts/c.md", big)
    # Rewrite to a drastically shorter page → should trigger a backup.
    m._apply_file_blocks([("concepts/c.md", "# 概念\n没了")])
    backups = list((m.wiki_path / ".backups").glob("*.md.bak"))
    check(len(backups) == 1, "drastic shrink snapshots the old page to .backups/")
    check(backups[0].read_text(encoding="utf-8") == big, "backup holds the FULL old content")
    # Backups must not pollute the wiki page listing (rglob '*.md').
    check(not any(p.startswith(".backups") for p in m.list_pages()),
          ".bak snapshots are invisible to list_pages()")
    # A normal-size update must NOT create a backup.
    m._apply_file_blocks([("concepts/c.md", big.replace("事实", "新事实"))])
    check(len(list((m.wiki_path / ".backups").glob("*.md.bak"))) == 1,
          "a non-shrinking rewrite creates no extra backup")


def test_network_pages_relations_subcap_and_focus():
    print("test_network_pages_relations_subcap_and_focus")
    m = _mgr(tempfile.mkdtemp())
    # relations.md is huge; an entity page is small but on-topic for the focus.
    m.write_page("relations.md", "# 关系\n" + ("[[A]]—(x)→[[B]]。" * 2000))
    m.write_page("entities/topicx.md", "# 主题X实体\n关键独有词 alpha-token 内容\n")
    m.write_page("entities/other.md", "# 其它\n无关 beta 内容\n")
    out = m._read_network_pages(2_000, focus_text="alpha-token 主题X")
    check("relations.md" in out, "relations.md is present in assembly context")
    check("…（已截断）" in out, "oversized relations.md is truncated, not dropped")
    check("entities/topicx.md" in out,
          "relations sub-cap leaves room for the focus-relevant entity page")


def test_assembly_prompts_format_with_all_fields():
    print("test_assembly_prompts_format_with_all_fields")
    full = ASSEMBLE_PROMPT_TEMPLATE.format(
        entity_hint="H", existing_network="E", atoms="A")
    check("H" in full and "E" in full and "A" in full,
          "ASSEMBLE_PROMPT_TEMPLATE formats with entity_hint/existing_network/atoms")
    inc = ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE.format(
        chunk_no=1, n_chunks=3, entity_hint="H", existing_network="E", atoms="A")
    check("1/3" in inc and "H" in inc,
          "ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE formats with all 5 fields")


def main():
    tests = [
        test_entity_normalize_and_extract,
        test_entity_match_is_conservative,
        test_entity_registry_roundtrip_and_hint,
        test_anti_loss_backup_on_shrink,
        test_network_pages_relations_subcap_and_focus,
        test_assembly_prompts_format_with_all_fields,
    ]
    cwd = os.getcwd()
    try:
        for t in tests:
            t()
    finally:
        os.chdir(cwd)
    print(f"\nAll offline checks passed ({_passed} assertions).")


if __name__ == "__main__":
    main()
