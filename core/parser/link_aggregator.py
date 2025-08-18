from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from core.log_utils import get_logger
from core.parser.web import fetch_url_html
from core.paths import CONFIG_JSON

logger = get_logger("link_aggregator")

# Конфиг из core/paths.py
CONFIG_PATH = CONFIG_JSON


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
    SOCIAL_PATTS = {
        "twitterURL": re.compile(r"(?:^|//)(?:www\.)?(?:twitter\.com|x\.com)/", re.I),
        "discordURL": re.compile(r"(?:^|//)(?:www\.)?discord\.(?:gg|com)/", re.I),
        "telegramURL": re.compile(r"(?:^|//)(?:www\.)?(?:t\.me|telegram\.me)/", re.I),
        "youtubeURL": re.compile(
            r"(?:^|//)(?:www\.)?(?:youtube\.com|youtu\.be)/", re.I
        ),
        "linkedinURL": re.compile(r"(?:^|//)(?:www\.)?linkedin\.com/", re.I),
        "redditURL": re.compile(r"(?:^|//)(?:www\.)?reddit\.com/", re.I),
        "mediumURL": re.compile(r"(?:^|//)(?:www\.)?medium\.com/", re.I),
        "githubURL": re.compile(r"(?:^|//)(?:www\.)?github\.com/", re.I),
    }
    soup = BeautifulSoup(html or "", "html.parser")

    out = {k: "" for k in list(SOCIAL_PATTS.keys()) + ["websiteURL"]}
    candidates_all: List[tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        raw = a["href"]
        href = force_https(urljoin(base_url, raw))
        href = _unwrap_redirect(href)
        href = force_https(href)
        txt = a.get_text(" ", strip=True) or ""
        candidates_all.append((href, txt))

        # соцсети
        for key, patt in SOCIAL_PATTS.items():
            if not out[key] and patt.search(href):
                out[key] = href

    # website по метке
    website = ""
    for href, txt in candidates_all:
        if (txt or "").strip().lower() in ("website", "official website", "site"):
            website = href
            break

    if not website:

        def _is_social(u: str) -> bool:
            return any(p.search(u) for p in SOCIAL_PATTS.values())

        for href, _ in candidates_all:
            if (
                href.startswith("http")
                and (not _is_social(href))
                and (not is_link_aggregator(href))
            ):
                website = href
                break

    # фолбэк - base_url
    out["websiteURL"] = website or force_https(base_url)
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
    html = fetch_url_html(agg_url, prefer="auto", timeout=30)
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
    # сырой html без браузера
    html = fetch_url_html(agg_url, prefer="http", timeout=30)
    if not html:
        return False, {}

    ok = False

    # простая проверка: домен донора встречается в html
    if site_domain:
        patt = re.compile(rf"\b{re.escape(site_domain)}\b", re.I)
        if patt.search(html):
            ok = True

    # опционально: тот же твиттер-хэндл тоже подтверждает принадлежность
    if not ok and twitter_handle:
        patt_x = re.compile(
            rf"https?://(?:www\.)?(?:x\.com|twitter\.com)/{re.escape(twitter_handle)}(?:/|$)",
            re.I,
        )
        if patt_x.search(html):
            ok = True

    if not ok:
        return False, {}

    # возврат минимум: websiteURL → корень донора
    homepage = f"https://www.{site_domain}/" if site_domain else ""
    socials = {"websiteURL": homepage} if homepage else {}

    logger.debug(
        "Агрегатор подтвержден простой проверкой: %s (match=%s)",
        force_https(agg_url),
        site_domain or twitter_handle or "—",
    )
    return True, socials


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
