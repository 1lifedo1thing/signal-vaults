"""Reddit 信息源：公开 Atom RSS（免费、无需 key、无需登录）。

要点（实测结论, 勿回退）:
- 国内网络 reddit.com 被墙, 必须走代理: REDDIT_PROXY（缺省回落 PUSH_PROXY）
- *.json 端点被 Reddit 反爬硬挡(403 Blocked), 不要再试, 只走 .rss Atom feed
- Reddit 对连发请求限流(429), 每个子版块之间必须 sleep 间隔, 失败退避重试 1 次
- urllib 经此代理会有 SSL EOF 抖动, 统一用 requests（本仓库已依赖）
"""
import os
import re
import time
import xml.etree.ElementTree as ET

import requests

from .base import Source

_NS = {"a": "http://www.w3.org/2005/Atom"}
_UA = "signal-vaults-digest/1.0 (personal research tool)"


def _strip_html(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


class RedditSource(Source):
    name = "reddit"

    def __init__(self, subs=None, sort="top", timeframe="day", limit_per_sub=25):
        # 默认含 YC 生态: r/ycombinator + r/startups (HN 即 YC 的新闻站, 已单独覆盖)
        self.subs = subs or ["LocalLLaMA", "programming", "machinelearning",
                             "ycombinator", "startups"]
        self.sort = sort            # top | new | hot
        self.timeframe = timeframe  # day | week | month (仅 top 有效)
        self.limit_per_sub = limit_per_sub

    def _fetch_one(self, sub):
        tf = "?t={}".format(self.timeframe) if self.sort == "top" else ""
        url = "https://www.reddit.com/r/{}/{}/.rss{}&limit={}".format(
            sub, self.sort, tf, self.limit_per_sub)
        proxy = os.environ.get("REDDIT_PROXY") or os.environ.get("PUSH_PROXY") or ""
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(url, headers={"User-Agent": _UA},
                         proxies=proxies, timeout=(10, 30))
        if r.status_code == 429:
            time.sleep(8)
            r = requests.get(url, headers={"User-Agent": _UA},
                             proxies=proxies, timeout=(10, 30))
        r.raise_for_status()
        return ET.fromstring(r.text)

    def fetch(self, days=1):
        items, errors = [], 0
        for i, sub in enumerate(self.subs):
            if i:
                time.sleep(5)  # reddit 限流: 子版块间必须间隔
            try:
                root = self._fetch_one(sub)
            except Exception:
                errors += 1
                continue
            for e in root.findall("a:entry", _NS)[: self.limit_per_sub]:
                link_el = e.find("a:link", _NS)
                items.append({
                    "title": (e.findtext("a:title", "", _NS) or "").strip(),
                    "url": link_el.attrib.get("href", "") if link_el is not None else "",
                    "summary": "r/{} | {}".format(
                        sub, _strip_html(e.findtext("a:content", "", _NS))[:150]),
                    "meta": self.name,
                })
        if errors and errors >= len(self.subs):
            print("[reddit] 全部子版块拉取失败 — reddit.com 需要代理: "
                  "在 .env 设 REDDIT_PROXY=http://127.0.0.1:端口 (可复用 PUSH_PROXY)")
        elif errors:
            print("[reddit] {}/{} 个子版块拉取失败(限流或网络), 已跳过".format(
                errors, len(self.subs)))
        return items



