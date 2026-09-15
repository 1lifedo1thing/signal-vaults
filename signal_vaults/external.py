"""外部信息源日报（Hacker News / Reddit）→ 同一套 LLM 提炼/渲染/推送管线。"""
import os
import time

from . import config, llm, daily
from .sources import HackerNewsSource, RedditSource


def _digest_for_source(source_name, items, days):
    """把 Item 列表走 LLM 精选, 复用微信管线的 merge/render。

    Item: {title, url, summary, meta}
    """
    import json, os
    from .sources import fmt_items
    # 主题/风格: SIG_VAULTS_TOPIC 追加关注主题; SIG_VAULTS_STYLE 追加摘要风格
    topic = os.environ.get("SIG_VAULTS_TOPIC", "").strip()
    style = os.environ.get("SIG_VAULTS_STYLE", "").strip()
    extra = ""
    if topic:
        extra += "【主题聚焦】优先保留与以下主题相关的条目, 无关的丢弃: {}。".format(topic)
    if style:
        extra += "【摘要风格】{}".format(style)
    prompt = (
        "你是AI前沿知识筛选员。以下是 {src} 近{days}天的条目列表(标题|链接|热度|摘要)。"
        "只保留AI/LLM/Agent/编程/技术相关且有信息量的条目, 每条给1-2句中文摘要(是什么/为什么值得看)。"
        "{extra}"
        "【链接铁律】url 必须逐字复制原文条目里的链接, 严禁构造。"
        "只输出JSON: {{\"knowledge\":[{{\"topic\":\"条目标题\",\"detail\":\"1-2句摘要\",\"who\":\"{src}\"}}],"
        "\"resources\":[{{\"title\":\"条目标题\",\"url\":\"原文链接\"}}]}}".format(
            src=source_name, days=days, extra=extra))
    text = fmt_items(items, source_name)
    raw = llm.chat(text[:16000], system=prompt)
    from .daily import _parse_llm_json
    data = _parse_llm_json(raw)
    raw_urls = {it["url"] for it in items if it.get("url")}
    hot, res = [], []
    for k in data.get("knowledge", []):
        hot.append({"topic": k.get("topic", ""), "detail": k.get("detail", ""),
                    "who": k.get("who", source_name)})
    for r in data.get("resources", []):
        u = (r.get("url") or "").strip()
        if u and not any(u == ru or u in ru or ru in u for ru in raw_urls):
            print("    [链接校验] 丢弃非来源 URL: {}".format(u[:60]))
            r = dict(r)
            r.pop("url", None)
        res.append(r)
    return {"hot": hot, "resources": res,
            "meta": {"chat": source_name, "days": days, "total": len(items),
                     "days_label": "近{}天".format(days),
                     "kind": "source",
                     "raw_chat": None, "thumbs": [], "files": []}}


def build_source_digest(source="hn", days=1, subs=None, limit=40):
    """拉取+提炼, 返回 (digest, txt_path); 不推送 — 供 CLI 和飞书 WS 复用"""
    if source in ("hn", "hacker-news", "hackernews"):
        src, name, fname = HackerNewsSource(limit=limit), "Hacker News", "hn"
    elif source == "reddit":
        kwargs = {"subs": subs} if subs else {}
        src, name, fname = RedditSource(**kwargs), "Reddit", "reddit"
    else:
        raise ValueError("未知信息源: {}".format(source))
    print("=== {} 条目采集 (近{}天) ===".format(name, days), flush=True)
    items = src.fetch(days)
    print("  拉取 {} 条".format(len(items)), flush=True)
    if not items:
        return None, None
    d = _digest_for_source(name, items, days)
    txt_path = os.path.join(config.WORK_DIR, "know_{}.txt".format(fname))
    open(txt_path, "w", encoding="utf-8").write(daily.render_text(d))
    print("  已写 {}".format(txt_path), flush=True)
    return d, txt_path


def run_source(source="hn", days=1, subs=None, limit=40):
    """signal-vaults hn / reddit 子命令入口。"""
    d, txt_path = build_source_digest(source, days, subs, limit)
    if d is None:
        print("  无条目")
        return 0
    st = daily.push_discord(d, txt_path)
    from . import feishu
    feishu.push_feishu(d, txt_path)
    print("-> Discord HTTP {} ({}条精选)".format(st, len(d["hot"])), flush=True)
    return 0
