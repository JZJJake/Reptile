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
    CROSS_SYNTHESIS_PROMPT_TEMPLATE,
)
from wiki.retrieval import VectorIndex, tokenize  # noqa: E402

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


def _atom(title, tags, ents):
    return (f"# {title}\n- 来源: {title}.md\n- 核心主张:\n  - x\n"
            f"- 关键实体: {ents}\n- 概念标签: {tags}\n- 潜在关联: 无\n")


def test_atom_features():
    print("test_atom_features")
    m = _mgr(tempfile.mkdtemp())
    feats = m._atom_features(_atom("t", "新能源, 储能、电池", "宁德时代；比亚迪"))
    check(m._normalize_entity("新能源") in feats, "concept tags become features")
    check(m._normalize_entity("宁德时代") in feats, "key entities become features")
    check(m._atom_features(_atom("t", "无", "无")) == set(),
          "the '无' sentinel yields no features")


def test_cluster_by_affinity_groups_by_topic():
    print("test_cluster_by_affinity_groups_by_topic")
    m = _mgr(tempfile.mkdtemp())
    # Two clear topics; atom names interleaved to prove grouping is by topic,
    # not by file order.
    specs = {
        "a_ev1.md":   _atom("电动车补贴", "新能源,电动车", "工信部"),
        "b_chip1.md": _atom("芯片制程",   "半导体,芯片", "台积电"),
        "c_ev2.md":   _atom("电池技术",   "新能源,电池", "宁德时代"),
        "d_chip2.md": _atom("光刻机",     "半导体,光刻", "阿斯麦"),
        "e_ev3.md":   _atom("充电桩",     "新能源,电动车", "国家电网"),
    }
    for name, body in specs.items():
        m.write_page(f"atoms/{name}", body)
    atoms = m._list_atoms()
    clusters = m._cluster_atoms_by_affinity(atoms, budget_tokens=10_000)
    names = [sorted(p.name for p in c) for c in clusters]
    ev = next((s for s in names if "a_ev1.md" in s), [])
    chip = next((s for s in names if "b_chip1.md" in s), [])
    check(set(ev) == {"a_ev1.md", "c_ev2.md", "e_ev3.md"},
          "all 新能源 atoms cluster together regardless of filename order")
    check(set(chip) == {"b_chip1.md", "d_chip2.md"},
          "all 半导体 atoms cluster together, separate from 新能源")


def test_cluster_respects_budget():
    print("test_cluster_respects_budget")
    m = _mgr(tempfile.mkdtemp())
    for i in range(4):
        m.write_page(f"atoms/x{i}.md", _atom(f"同主题{i}", "同一主题", f"实体{i}"))
    atoms = m._list_atoms()
    # Tiny budget forces a split even though all atoms share a topic.
    clusters = m._cluster_atoms_by_affinity(atoms, budget_tokens=30)
    check(len(clusters) > 1, "a tiny token budget splits even same-topic atoms")
    check(sum(len(c) for c in clusters) == 4, "no atom is dropped when splitting")


def test_cross_synthesis_prompt_formats():
    print("test_cross_synthesis_prompt_formats")
    p = CROSS_SYNTHESIS_PROMPT_TEMPLATE.format(
        relations_content="R", index_content="I", existing_synthesis="S")
    check("R" in p and "I" in p and "S" in p, "synthesis prompt formats with 3 fields")
    check("[推断]" in p, "synthesis prompt enforces the [推断] no-fabrication tag")


def test_tokenize_cjk_and_ascii():
    print("test_tokenize_cjk_and_ascii")
    toks = tokenize("新能源汽车 EV battery")
    check("新能" in toks and "能源" in toks, "CJK text yields character bigrams")
    check("battery" in toks and "ev" in toks, "ASCII words are lowercased tokens")
    check("a" not in tokenize("a 我"), "single ASCII chars are dropped (len<2)")


def test_vector_index_ranks_relevant_first():
    print("test_vector_index_ranks_relevant_first")
    docs = {
        "ev.md":    "# 电动车补贴 新能源汽车 电池 续航 充电",
        "chip.md":  "# 半导体 芯片 光刻机 制程 台积电",
        "policy.md": "# 产业政策 补贴 财政 税收",
    }
    idx = VectorIndex.build(docs)
    hits = idx.search("新能源汽车电池续航", top_k=3)
    check(hits[0][0] == "ev.md", "the on-topic doc ranks first by cosine")
    check(hits[0][1] > 0, "top hit has positive similarity")
    # An unrelated query returns the least-bad match but low score; a no-overlap
    # query returns nothing.
    check(idx.search("量子计算 zzz", top_k=3) == [],
          "a query with no term overlap returns no hits")


def test_find_relevant_pages_uses_vector_ranking():
    print("test_find_relevant_pages_uses_vector_ranking")
    m = _mgr(tempfile.mkdtemp())
    m.write_page("concepts/ev.md", "# 新能源汽车\n电池 续航 充电桩 补贴 三电系统")
    m.write_page("concepts/chip.md", "# 半导体\n芯片 光刻机 制程 EUV")
    out = m._find_relevant_pages("新能源汽车的续航和充电问题")
    check("concepts/ev.md" in out, "vector page-select surfaces the relevant page")
    check(out.index("concepts/ev.md") < (out.index("concepts/chip.md")
          if "concepts/chip.md" in out else len(out)),
          "the relevant page is ranked ahead of the off-topic one")


def main():
    tests = [
        test_entity_normalize_and_extract,
        test_entity_match_is_conservative,
        test_entity_registry_roundtrip_and_hint,
        test_anti_loss_backup_on_shrink,
        test_network_pages_relations_subcap_and_focus,
        test_assembly_prompts_format_with_all_fields,
        test_atom_features,
        test_cluster_by_affinity_groups_by_topic,
        test_cluster_respects_budget,
        test_cross_synthesis_prompt_formats,
        test_tokenize_cjk_and_ascii,
        test_vector_index_ranks_relevant_first,
        test_find_relevant_pages_uses_vector_ranking,
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
