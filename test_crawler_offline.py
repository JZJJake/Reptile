"""Offline regression tests for the crawler's dependency-free quality heuristics
(content_filter.py). Run with:  python test_crawler_offline.py

No Playwright / bs4 / network — pure stdlib logic, so these run anywhere.
"""

import content_filter as cf
from content_filter import (
    should_skip_url, url_priority, is_low_quality_content,
    PRIORITY_ARTICLE, PRIORITY_LIST, PRIORITY_DEFAULT,
)

_passed = 0


def check(cond, label):
    global _passed
    if cond:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        raise AssertionError(f"FAILED: {label}")


def test_should_skip_url():
    print("test_should_skip_url")
    for u in [
        "https://site.com/user/login",
        "https://site.com/search?q=x",
        "https://site.com/article?action=share",
        "javascript:void(0)",
        "mailto:a@b.com",
        "https://site.com/feed/rss",
        "https://site.com/wp-login.php",
    ]:
        check(should_skip_url(u), f"skips junk URL: {u}")
    for u in [
        "https://site.com/news/2024/05/18/123456.html",
        "https://site.com/content/detail/987654",
        "https://site.com/list/page/2",      # list pagination still allowed
    ]:
        check(not should_skip_url(u), f"keeps content/discovery URL: {u}")


def test_url_priority_ordering():
    print("test_url_priority_ordering")
    art = url_priority("https://site.com/content/2024/05/123456.html")
    lst = url_priority("https://site.com/news/list/")
    default = url_priority("https://site.com/about-us")
    check(art == PRIORITY_ARTICLE, "article/detail page gets ARTICLE priority")
    check(lst == PRIORITY_LIST, "list/index page gets LIST priority")
    check(art > default > lst,
          "article crawled before default before list (article-first ordering)")


def test_low_quality_rejects_nav_and_stubs():
    print("test_low_quality_rejects_nav_and_stubs")
    low, _ = is_low_quality_content("首页 关于我们 联系我们 登录")
    check(low, "short nav strip is rejected")
    low, reason = is_low_quality_content("内容不错。", )
    check(low and reason.startswith("too_short"), "below min length rejected")
    # Menu/list shape: many short lines, no sentences.
    menu = "\n".join(["首页", "新闻", "公告", "通知", "下载", "登录", "注册", "关于", "联系"])
    low, _ = is_low_quality_content(menu)
    check(low, "menu of short lines is rejected as list-like")
    # High link density flagged even when the text clears the length gate.
    longtext = "这是一段看起来还行的文字内容。" * 24
    low, reason = is_low_quality_content(longtext, link_density=0.8)
    check(low and reason.startswith("link_heavy"), "high link density rejected")


def test_low_quality_accepts_real_article():
    print("test_low_quality_accepts_real_article")
    article = (
        "近日，国家发展改革委发布了关于新能源汽车产业发展的指导意见。"
        "意见指出，要加快充电基础设施建设，完善动力电池回收体系，"
        "并对关键核心技术攻关给予专项资金支持。文件还明确了未来三年的阶段性目标。\n\n"
        "业内专家认为，这一政策将显著推动行业的健康发展，并带动上下游产业链协同升级。"
        "同时，地方政府也应出台配套措施，确保政策落地见效，避免出现重复建设和资源浪费。"
        "多家整车企业表示，将加大研发投入，提升产品竞争力，以适应新的市场环境与监管要求。"
    )
    low, reason = is_low_quality_content(article, link_density=0.1)
    check(not low, f"a real paragraphed article passes the gate (reason={reason})")


def test_content_hash_dedup_logic_constants():
    print("test_content_hash_dedup_logic_constants")
    # The gate thresholds should stay sane (guard against accidental edits).
    check(cf.MIN_CONTENT_CHARS >= 150, "min content length stays meaningfully high")
    check(0.4 < cf.LINK_DENSITY_MAX < 0.8, "link-density threshold in a sane band")


def main():
    for t in (test_should_skip_url, test_url_priority_ordering,
              test_low_quality_rejects_nav_and_stubs,
              test_low_quality_accepts_real_article,
              test_content_hash_dedup_logic_constants):
        t()
    print(f"\nAll crawler offline checks passed ({_passed} assertions).")


if __name__ == "__main__":
    main()
