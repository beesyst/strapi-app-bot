from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from core.log_utils import get_logger
from core.parser_web import fetch_url_html

logger = get_logger("link_aggregator")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")


# Перевод url в https
def force_https(url: str) -> str:
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Возврат домена без www из url
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Загрузка списка доменов-агрегаторов из config.json
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
    LINK_COLLECTION_DOMAINS = {
        (d or "").lower().replace("www.", "")
        for d in _cfg.get("link_collections", [])
        if d
    }
except Exception as e:
    logger.warning("link_aggregator: ошибка чтения config: %s", e)
    LINK_COLLECTION_DOMAINS = set()


# Проверка ссылки линк-агрегатора
def is_link_aggregator(url: str) -> bool:
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in LINK_COLLECTION_DOMAINS)


# Соцсети из HTML без фильтрации агрегаторов
def extract_socials_raw_from_html(html: str, base_url: str) -> Dict[str, str]:
    PATTERNS = {
        "twitterURL": re.compile(r"twitter\.com|x\.com", re.I),
        "discordURL": re.compile(r"discord\.gg|discord\.com", re.I),
        "telegramURL": re.compile(r"t\.me|telegram\.me", re.I),
        "youtubeURL": re.compile(r"youtube\.com|youtu\.be", re.I),
        "linkedinURL": re.compile(r"linkedin\.com", re.I),
        "redditURL": re.compile(r"reddit\.com", re.I),
        "mediumURL": re.compile(r"medium\.com", re.I),
        "githubURL": re.compile(r"github\.com", re.I),
        "websiteURL": re.compile(r"^https?://", re.I),
    }
    soup = BeautifulSoup(html or "", "html.parser")
    out = {k: "" for k in PATTERNS}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_href = force_https(urljoin(base_url, href))
        abs_href = _unwrap_redirect(abs_href)
        abs_href = force_https(abs_href)
        for key, patt in PATTERNS.items():
            if patt.search(abs_href):
                out[key] = abs_href
    out["websiteURL"] = out.get("websiteURL") or force_https(base_url)
    return out


# Хелпер развертки
def _unwrap_redirect(u: str) -> str:
    try:
        p = urlparse(u)
        qs = parse_qs(p.query)
        for key in ("url", "target", "u", "to", "dest"):
            if key in qs and qs[key]:
                cand = unquote(qs[key][0])
                if cand.startswith("http"):
                    return cand
        if "/url/" in p.path:
            tail = p.path.split("/url/", 1)[1]
            cand = unquote(tail)
            if cand.startswith("http"):
                return cand
    except Exception:
        pass
    return u


# Соцсети из агрегатора, убирая другие агрегаторы
def extract_socials_from_aggregator(agg_url: str) -> Dict[str, str]:
    html = fetch_url_html(agg_url, prefer="auto", timeout=30)  # общий fetch
    if not html:
        return {}
    socials = extract_socials_raw_from_html(html, agg_url)
    for k, v in list(socials.items()):
        if v and is_link_aggregator(v):
            socials.pop(k, None)
    return socials


# Проверка совпадения хоста с доменом проекта
def _host_matches_domain(h: str, site_domain: str) -> bool:
    h = (h or "").lower().replace("www.", "")
    sd = (site_domain or "").lower().replace("www.", "")
    return bool(h == sd or h.endswith("." + sd))


# Агрегатор относится к проекту
def verify_aggregator_belongs(
    agg_url: str, site_domain: str, twitter_handle: str | None
) -> Tuple[bool, Dict[str, str]]:
    html = fetch_url_html(agg_url, prefer="auto", timeout=30)  # общий fetch
    if not html:
        return False, {}

    socials = extract_socials_raw_from_html(html, agg_url)

    site_match = any(
        _host_matches_domain(_host(v), site_domain) for v in socials.values() if v
    )

    same_x = False
    h = (twitter_handle or "").strip().lower()
    if h:
        p = re.compile(
            rf"https?://(?:www\.)?(?:x\.com|twitter\.com)/{re.escape(h)}(?:/|$)", re.I
        )
        if p.search(html):
            same_x = True

    ok = bool(site_match or same_x)
    if ok:
        logger.info("Агрегатор подтвержден: %s", force_https(agg_url))
    return ok, socials


# Фильтр и возврат списка уникальных агрегаторов из ссылок
def find_aggregators_in_links(links: List[str]) -> List[str]:
    aggs = []
    for b in links or []:
        if is_link_aggregator(b):
            aggs.append(force_https(b))
    seen, out = set(), []
    for u in aggs:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
