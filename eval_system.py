"""
Reptile system end-to-end evaluation script.
Run this locally where (a) gov.cn is accessible and (b) DeepSeek API key works.

Usage:
    python eval_system.py --api-key YOUR_DEEPSEEK_KEY --url https://www.ndrc.gov.cn/xwdt/xwfb/
"""
import argparse, asyncio, json, time, httpx, sys

BASE = "http://localhost:8000"

async def poll_until(url, done_fn, interval=15, timeout=600):
    start = time.time()
    async with httpx.AsyncClient(timeout=30) as c:
        while time.time() - start < timeout:
            r = await c.get(url)
            d = r.json()
            if done_fn(d): return d
            await asyncio.sleep(interval)
    return None

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", required=True)
    p.add_argument("--url", default="https://www.ndrc.gov.cn/xwdt/xwfb/")
    p.add_argument("--pages", type=int, default=20)
    args = p.parse_args()

    results = {}
    async with httpx.AsyncClient(timeout=60, base_url=BASE) as c:

        print("\n=== STEP 1: Crawl ===")
        r = await c.post("/api/scrape/start", json={
            "url": args.url, "api_key": args.api_key,
            "single_page": False, "date_from": "", "update_data": True
        })
        task = r.json()
        task_id = task["task_id"]
        print(f"  task_id: {task_id}")

        d = await poll_until(f"{BASE}/api/scrape/status/{task_id}",
            lambda d: d["total_scraped"] >= args.pages or d["status"] in ("completed","failed"),
            interval=20, timeout=900)
        scraped = d["total_scraped"] if d else 0
        status  = d["status"] if d else "timeout"
        results["crawl"] = {"scraped": scraped, "status": status}
        print(f"  Scraped: {scraped} pages, status: {status}")

        if scraped == 0:
            print("  ⚠️  No pages scraped — wiki build skipped"); sys.exit(1)

        # Derive domain
        from urllib.parse import urlparse
        domain = urlparse(args.url).netloc.replace("www.", "")

        print(f"\n=== STEP 2: Wiki build (domain={domain}) ===")
        await c.post("/api/wiki/build", json={"domain": domain, "api_key": args.api_key})
        time.sleep(3)
        d2 = await poll_until(f"{BASE}/api/wiki/status/{domain}",
            lambda d: d.get("page_count", 0) > 0, interval=20, timeout=300)
        pages = d2["page_count"] if d2 else 0
        results["wiki"] = {"pages": pages, "last_op": d2.get("last_operation") if d2 else None}
        print(f"  Wiki pages built: {pages}")

        print("\n=== STEP 3: Q&A accuracy (5 questions) ===")
        questions = [
            "最近发布了哪些重要政策？",
            "政府在经济方面有哪些重要措施？",
            "民生保障方面有什么政策？",
            "科技创新政策有哪些新举措？",
            "能源和环保方面有哪些最新部署？",
        ]
        qa_results = []
        for q in questions:
            r3 = await c.post("/api/wiki/query",
                json={"question": q, "domain": domain, "api_key": args.api_key, "stream": False})
            ans = r3.json().get("answer", "")
            non_empty  = len(ans.strip()) > 50
            has_cite   = "[[" in ans
            not_found  = "未找到" in ans and len(ans) < 200
            relevant   = non_empty and not not_found
            qa_results.append({
                "q": q, "len": len(ans), "non_empty": non_empty,
                "has_cite": has_cite, "relevant": relevant
            })
            sym = "✅" if relevant else "❌"
            print(f"  {sym} [{len(ans)}字] 引用={'是' if has_cite else '否'} | {q}")
            if ans:
                print(f"     摘要: {ans[:120]}...")
        results["qa"] = qa_results

    # Summary
    print("\n" + "="*60)
    print("EVALUATION REPORT")
    print("="*60)
    print(f"爬取:    {results['crawl']['scraped']} 页 ({results['crawl']['status']})")
    print(f"知识库:  {results['wiki']['pages']} 页")
    qa = results["qa"]
    passed = sum(1 for r in qa if r["relevant"])
    cited  = sum(1 for r in qa if r["has_cite"])
    print(f"问答准确: {passed}/{len(qa)} 通过, 其中 {cited} 个有[[引用]]")
    verdict = "✅ PASS" if passed >= 4 and results['wiki']['pages'] > 5 else "⚠️  NEEDS IMPROVEMENT"
    print(f"总体评定: {verdict}")

asyncio.run(main())
