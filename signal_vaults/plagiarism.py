# -*- coding: utf-8 -*-
"""长文抄袭/搬运检测 (v1.1.0)

流程: 本地文章 → LLM提取指纹探针句(逐字子串校验) → 多平台精确搜索
      → 抓取候选页 → shingle包含度+探针命中率 → 告警卡片推送

探针铁律: 探针必须是原文的逐字子串(防LLM改写); 命中铁律: 比对分数只能
来自真实抓取的页面内容, 不臆造相似度。链接只来自搜索引擎真实结果。
"""
import os
import re
import time

import requests

from . import config, llm, daily

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _proxy():
    p = os.environ.get("CHECK_PROXY") or os.environ.get("PUSH_PROXY") or ""
    return {"http": p, "https": p} if p else None


def load_article(path):
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return open(path, encoding=enc).read()
        except Exception:
            continue
    raise ValueError("无法读取: {}".format(path))


# ---------- 1. 指纹探针 ----------

def _probes_fallback(text, n=6):
    """无LLM兜底: 取含专有名词/数字/标点的最长句段"""
    sents = re.split(r"[。！？!?\n]", text)
    sents = [s.strip() for s in sents if 25 <= len(s.strip()) <= 80]
    sents.sort(key=lambda s: (len(re.findall(r"[A-Za-z0-9·]", s)), len(s)), reverse=True)
    return sents[:n]


def extract_probes(text, n=6):
    """LLM从原文挑n段独特探针(20-60字), 逐字校验是原文子串, 不合格自动淘汰"""
    body = text[:6000]
    prompt = (
        "你是内容指纹提取器。从下面文章里挑{}段「独特探针句」用于全网搬运检测:\n"
        "1. 每段20-60个连续字符, 必须逐字复制原文(一字不改)\n"
        "2. 优先选: 含专有名词/数字/术语/罕见搭配的句子, 避免通用句式\n"
        "3. 只输出JSON: {{\"probes\":[\"探针1\",\"探针2\"]}}\n\n文章:\n".format(n)) + body
    probes = []
    try:
        raw = llm.chat("提取探针", system=prompt)
        from .daily import _parse_llm_json
        probes = [p.strip() for p in _parse_llm_json(raw).get("probes", []) if p.strip()]
    except Exception as e:
        print("  LLM探针失败, 用兜底句: {}".format(str(e)[:60]))
    probes = [p for p in probes if p in text][:n]
    if len(probes) < 3:
        probes = list({*probes, *_probes_fallback(text, n)})[:n]
    return [p for p in probes if p in text]


# ---------- 2. 探针搜索 ----------

def _search_sogou_mp(probe):
    """搜狗微信文章搜索 (公众号搬运最准, 无需key)"""
    from urllib.parse import quote
    url = "https://weixin.sogou.com/weixin?type=2&query={}".format(quote('"' + probe + '"'))
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=(10, 20))
        r.raise_for_status()
        out = []
        for m in re.finditer(r'<h3>\s*<a[^>]*href="(/link\?url=[^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            out.append({"title": title, "url": "https://weixin.sogou.com" + m.group(1),
                        "platform": "公众号(搜狗)"})
        return out[:5]
    except Exception as e:
        print("  [搜狗] 失败: {}".format(str(e)[:60]))
        return []


def _search_ddg(probe, site=None):
    """DuckDuckGo HTML 精确短语搜索 (需代理或海外网络)"""
    q = '"{}"'.format(probe)
    if site:
        q += " site:{}".format(site)
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": q}, headers={"User-Agent": UA},
                          proxies=_proxy(), timeout=(10, 25))
        r.raise_for_status()
        out = []
        for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>',
                             r.text, re.S):
            href = m.group(1)
            # ddg 重定向链接 //duckduckgo.com/l/?uddg=<真实url>
            um = re.search(r"uddg=([^&]+)", href)
            if um:
                from urllib.parse import unquote
                href = unquote(um.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            out.append({"title": title, "url": href, "platform": "网页"})
        return out[:5]
    except Exception as e:
        print("  [DDG] 失败: {}".format(str(e)[:60]))
        return []


def search_platforms(probe):
    """公众号优先搜狗微信; 其他网页走 DDG。合并去重"""
    hits = _search_sogou_mp(probe)
    ddg = _search_ddg(probe)
    seen = {h["url"] for h in hits}
    for h in ddg:
        if h["url"] not in seen:
            hits.append(h)
            seen.add(h["url"])
    return hits


# ---------- 3. 相似度比对 ----------

def _shingles(t, k=12, step=3):
    t = re.sub(r"\s+", "", t)
    return {t[i:i + k] for i in range(0, max(1, len(t) - k), step)}


def _fetch_text(url, limit=120000):
    r = requests.get(url, headers={"User-Agent": UA}, proxies=_proxy(),
                     timeout=(10, 25))
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text[:limit * 4]
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", txt)[:limit]


def check_page(url, article_text, probes):
    """返回 (相似度0-1, 命中探针列表); 页面抓不到返回 None"""
    try:
        page = _fetch_text(url)
    except Exception as e:
        print("    抓取失败 {}: {}".format(url[:50], str(e)[:40]))
        return None
    sh = _shingles(article_text[:4000])
    pg = _shingles(page)
    contain = (sum(1 for s in sh if s in pg) / len(sh)) if sh else 0.0
    hit = [p for p in probes if p in page]
    return contain, hit


# ---------- 4. 主流程 ----------

def run_check(path, push=True):
    """signal-vaults check <文章.md>"""
    text = load_article(path)
    name = os.path.basename(path)
    print("=== 抄袭检测: {} ({}字) ===".format(name, len(text)), flush=True)
    probes = extract_probes(text)
    if not probes:
        print("  [!!] 无法提取探针(文章太短?)")
        return 1
    print("  探针{}条: {}".format(len(probes), " / ".join(p[:18] + "…" for p in probes[:3])))

    candidates, seen = [], set()
    for i, p in enumerate(probes, 1):
        print("  搜探针 {}/{} …".format(i, len(probes)), flush=True)
        for h in search_platforms(p):
            if h["url"] not in seen:
                candidates.append(h)
                seen.add(h["url"])
        time.sleep(1.5)  # 搜索引擎礼貌间隔
    print("  候选页面: {}".format(len(candidates)))

    hits = []
    for c in candidates:
        r = check_page(c["url"], text, probes)
        if not r:
            continue
        score, matched = r
        if score >= 0.12 or len(matched) >= 2:
            hits.append({**c, "score": score, "matched": matched})
            print("    [疑似] {:.0%} {} — {}".format(
                score, c["platform"], c["title"][:40]))
    hits.sort(key=lambda h: -h["score"])

    if not hits:
        print("  [OK] 未发现搬运")
        return 0

    hot = [{"topic": "{:.0%} 相似 — {}".format(h["score"], h["title"][:40]),
            "detail": "命中探针{}/{}: {} | {}".format(
                len(h["matched"]), len(probes), h["platform"], h["url"]),
            "who": h["platform"]} for h in hits[:8]]
    res = [{"title": h["title"][:60], "url": h["url"]} for h in hits[:8]]
    digest = {"hot": hot, "resources": res, "jargon": [],
              "meta": {"chat": "抄袭检测 · " + name, "days": 0,
                       "total": len(candidates)},
              "thumbs": [], "files": []}
    txt_path = os.path.join(config.WORK_DIR, "know_plagiarism.txt")
    open(txt_path, "w", encoding="utf-8").write(daily.render_text(digest))
    if push:
        st = daily.push_discord(digest, txt_path)
        from . import feishu
        feishu.push_feishu(digest, txt_path)
        print("-> Discord HTTP {} ({}个疑似搬运)".format(st, len(hits)))
    return 0
