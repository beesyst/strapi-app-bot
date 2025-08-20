from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger
from core.normalize import clean_project_name, is_bad_name
from core.paths import CONFIG_JSON

# Логгер
logger = get_logger("parser_web")

# Пути/конфиг
try:
    with open(CONFIG_JSON, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

# Кэш
FETCHED_HTML_CACHE: dict[str, str] = {}
PARSED_INTERNALS_CACHE: dict[str, list[str]] = {}
PARSED_DOCS_LINKS_LOGGED: set[str] = set()

# Регулярки для соцсетей
SOCIAL_PATTERNS = {
    "twitterURL": re.compile(r"twitter\.com|x\.com", re.I),
    "discordURL": re.compile(r"discord\.gg|discord\.com", re.I),
    "telegramURL": re.compile(r"t\.me|telegram\.me", re.I),
    "youtubeURL": re.compile(r"youtube\.com|youtu\.be", re.I),
    "linkedinURL": re.compile(r"linkedin\.com", re.I),
    "redditURL": re.compile(r"reddit\.com", re.I),
    "mediumURL": re.compile(r"medium\.com", re.I),
    "githubURL": re.compile(r"github\.com", re.I),
    "websiteURL": re.compile(
        r"^https?://(?!(?:www\.)?(?:twitter\.com|x\.com|discord\.gg|discord\.com|t\.me|telegram\.me|"
        r"youtube\.com|youtu\.be|linkedin\.com|reddit\.com|medium\.com|github\.com))",
        re.I,
    ),
    "documentURL": re.compile(r"docs\.", re.I),
}


# Host из URL без www
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Доменное имя верхнего уровня (без www), как строку netloc
def get_domain_name(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "").lower()
    except Exception:
        return url


# Имя проекта по многослойной стратегии
def extract_project_name(
    html: str,
    base_url: str,
    twitter_display_name: str = "",
) -> str:
    # приоритет: имя из X (display name)
    tw = clean_project_name(twitter_display_name or "")
    if tw and not is_bad_name(tw):
        return tw

    # если прилетел json от browser_fetch.js - пробуем поля тайтла/сайта
    try:
        j = json.loads(html or "{}")
        if isinstance(j, dict):
            for key in ("pageTitle", "title", "ogSiteName", "siteName"):
                val = clean_project_name(str(j.get(key, "")).strip())
                if val and not is_bad_name(val):
                    return val
    except Exception:
        pass

    soup = BeautifulSoup(html or "", "html.parser")

    # og:site_name
    meta_site = soup.select_one(
        "meta[property='og:site_name'][content], meta[name='og:site_name'][content]"
    )
    if meta_site and meta_site.get("content"):
        val = clean_project_name(meta_site.get("content", "").strip())
        if val and not is_bad_name(val):
            return val

    # <title> - выбираем лучшую часть
    domain_token = ""
    try:
        domain_token = urlparse(base_url).netloc.replace("www.", "").split(".")[0]
    except Exception:
        domain_token = ""

    raw_title = ""
    if soup.title and soup.title.string:
        raw_title = soup.title.string.strip()

    if raw_title:
        # разбиваем по частым разделителям
        parts = re.split(r"[|\-–—:•·⋅]+", raw_title)
        candidates = []
        for p in parts:
            val = clean_project_name(p or "")
            if not val or is_bad_name(val):
                continue
            score = 0
            # бонус за попадание доменного токена
            if domain_token and domain_token.lower() in val.lower():
                score += 100
            # умеренная длина 2..40 символов
            if 2 <= len(val) <= 40:
                score += 10
            # без лишних слов вроде "official", "homepage" и т.п. - их фильтрует is_bad_name
            candidates.append((score, val))

        if candidates:
            candidates.sort(key=lambda x: (-x[0], len(x[1])))
            best = candidates[0][1]
            if best and not is_bad_name(best):
                return best

    # header/nav: alt у логотипа → h1 (с фильтром мусора)
    header = soup.select_one("header") or soup.select_one("nav")
    if header:
        img = header.select_one("img[alt]")
        if img and img.get("alt"):
            val = clean_project_name(img.get("alt", "").strip())
            if val and not is_bad_name(val):
                return val
        h1 = header.select_one("h1")
        if h1 and h1.get_text(strip=True):
            val = clean_project_name(h1.get_text(strip=True))
            if val and not is_bad_name(val):
                return val

    # фолбэк - домен
    try:
        token = urlparse(base_url).netloc.replace("www.", "").split(".")[0]
        val = clean_project_name((token or "").capitalize())
        return val
    except Exception:
        return "Project"


# Json из browser_fetch.js, а не обычный html
def _looks_like_browser_json(s: str) -> bool:
    if not s or len(s) < 10:
        return False
    try:
        data = json.loads(s)
        return isinstance(data, dict) and "websiteURL" in data
    except Exception:
        return False


# Грубая эвристика "страница подозрительна/антибот"
def is_html_suspicious(html: str) -> bool:
    if not html:
        return True
    # cloudflare/антибот паттерны
    if (
        "cf-browser-verification" in html
        or "Cloudflare" in html
        or "Just a moment..." in html
        or "checking your browser" in html.lower()
        or "verifying you are human" in html.lower()
    ):
        return True
    # короткий HTML
    if len(html) < 2500:
        return True
    for dom in (
        "twitter.com",
        "x.com",
        "discord.gg",
        "t.me",
        "telegram.me",
        "github.com",
        "medium.com",
    ):
        if dom in html:
            return False
    return False


# В html хотя бы одна соцссылка по доменам
def has_social_links(html: str) -> bool:
    for dom in (
        "twitter.com",
        "x.com",
        "discord.gg",
        "t.me",
        "telegram.me",
        "github.com",
        "medium.com",
    ):
        if dom in html:
            return True
    return False


# Принудительный перевод любых http/протокол-relative ссылок в https
def force_https(url: str) -> str:
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Обертка поверх browser_fetch.js
def fetch_url_html_playwright(url: str, timeout: int = 60) -> str:
    script_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "browser_fetch.js"
    )

    def _run(args, label):
        try:
            result = subprocess.run(
                ["node", script_path, *args],
                cwd=os.path.dirname(script_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                logger.info("Playwright (%s) ок: %s", label, url)
                return result.stdout or ""
            logger.warning(
                "Playwright (%s) error for %s: %s",
                label,
                url,
                (result.stderr or "").strip(),
            )
            return result.stdout or result.stderr or ""
        except Exception as e:
            logger.warning("Playwright (%s) упал для %s: %s", label, url, e)
            return ""

    # Попытка 1: обычный режим
    out = _run([url], "normal")
    if out and out.strip():
        return out

    # Попытка 2: raw (возврат html/antiBot статус внутри json)
    out_raw = _run([url, "--raw"], "raw")
    return out_raw or out


# Основной fetch c политикой prefer=('auto'|'http'|'browser'), антибот-эвристики и кэш
def fetch_url_html(url: str, *, prefer: str = "auto", timeout: int = 30) -> str:
    # x.com/twitter.com
    h = _host(url)
    if h in ("x.com", "twitter.com"):
        prefer = "browser"

    # кэш по URL и стратегии
    if url in FETCHED_HTML_CACHE:
        return FETCHED_HTML_CACHE[url]

    # requests
    if prefer == "http":
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            resp = requests.get(
                url, headers=headers, timeout=timeout, allow_redirects=True
            )
            html = resp.text or ""
        except Exception as e:
            logger.warning("requests error %s: %s", url, e)
            html = ""
        FETCHED_HTML_CACHE[url] = html
        return html

    # браузер
    if prefer == "browser":
        out = fetch_url_html_playwright(url)
        FETCHED_HTML_CACHE[url] = out
        return out

    # auto: requests → браузер
    headers = {"User-Agent": "Mozilla/5.0"}
    html = ""
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        html = resp.text or ""
    except Exception as e:
        logger.warning("requests error %s: %s", url, e)

    if (not html) or is_html_suspicious(html):
        out = fetch_url_html_playwright(url)
        FETCHED_HTML_CACHE[url] = out or html
        return FETCHED_HTML_CACHE[url]

    FETCHED_HTML_CACHE[url] = html
    return html


# Лучшая docs-ссылка
def find_best_docs_link(soup: BeautifulSoup, base_url: str) -> str:
    candidates: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.text or "").strip().lower()
        href_full = urljoin(base_url, href)
        if any(
            k in text for k in ("docs", "documentation", "developer docs", "developers")
        ):
            candidates.append((text, href_full))

    filtered = [
        (text, href)
        for text, href in candidates
        if not any(
            skip in href
            for skip in (
                "api-docs",
                "developer-docs",
                "apidocs",
                "api/",
                "api.",
                "developers",
            )
        )
    ]

    def _score(href: str) -> int:
        parsed = urlparse(href)
        if re.match(r".*/docs/?$", parsed.path) and not parsed.netloc.startswith(
            "api."
        ):
            return 0
        if parsed.netloc.startswith("docs."):
            return 1
        return 2

    doc_url = ""
    if filtered:
        filtered.sort(key=lambda t: _score(t[1]))
        doc_url = filtered[0][1]
    else:
        all_hrefs = [
            urljoin(base_url, a["href"]) for a in soup.find_all("a", href=True)
        ]
        for href in all_hrefs:
            parsed = urlparse(href)
            if re.match(r".*/docs/?$", parsed.path) or parsed.netloc.startswith(
                "docs."
            ):
                doc_url = href
                break

    if doc_url:
        if doc_url not in PARSED_DOCS_LINKS_LOGGED:
            logger.info("docs-ссылка найдена: %s", doc_url)
            PARSED_DOCS_LINKS_LOGGED.add(doc_url)
        return doc_url
    return ""


# Парс всех соцссылок/док с html либо из json browser_fetch
def extract_social_links(html: str, base_url: str, is_main_page: bool = False) -> dict:
    try:
        j = json.loads(html)
        if isinstance(j, dict) and "websiteURL" in j:
            if j.get("error"):
                logger.warning("browser_fetch.js error in payload: %s", j.get("error"))
            else:
                if "twitterAll" in j and isinstance(j["twitterAll"], list):
                    logger.info("browser_fetch.js twitterAll: %d", len(j["twitterAll"]))
                logger.info("Соцлинки (browser_fetch.js): %s", j)
                # Приводим ключевые значения к https для единообразия
                for k, v in list(j.items()):
                    if isinstance(v, str):
                        j[k] = force_https(v)
                return j
    except Exception:
        pass

    # обычный html → парс соцссылок и docs
    soup = BeautifulSoup(html or "", "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}

    # кандидаты только из шапки/навигации/футера
    zones = []
    zones.extend(soup.select("header, nav"))
    zones.extend(soup.select("footer"))
    zones.append(soup.find(["div", "section"], recursive=False))
    zones.append(soup.select_one("body > :last-child"))

    def _scan_zone(node):
        if not node:
            return
        for a in node.find_all("a", href=True):
            abs_href = urljoin(base_url, a["href"])
            for key, pattern in SOCIAL_PATTERNS.items():
                if key == "documentURL":
                    continue
                if pattern.search(abs_href):
                    if not links.get(key):
                        links[key] = abs_href

    for z in zones:
        _scan_zone(z)

    # если по "зонам" пусто - проход по всей странице
    if all(not links[k] for k in links if k != "websiteURL"):
        for a in soup.find_all("a", href=True):
            abs_href = urljoin(base_url, a["href"])
            for key, pattern in SOCIAL_PATTERNS.items():
                if key == "documentURL":
                    continue
                if not links.get(key) and pattern.search(abs_href):
                    links[key] = abs_href

    # website и docs
    links["websiteURL"] = base_url
    doc_url = find_best_docs_link(soup, base_url)
    links["documentURL"] = doc_url or ""

    # если docs найден - дозаполняем пустые поля со страницы docs
    if doc_url:
        doc_html = fetch_url_html(doc_url, prefer="auto")
        dsoup = BeautifulSoup(doc_html or "", "html.parser")
        dzones = []
        dzones.extend(dsoup.select("header, nav"))
        dzones.extend(dsoup.select("footer"))
        dzones.append(dsoup.find(["div", "section"], recursive=False))
        dzones.append(dsoup.select_one("body > :last-child"))

        for z in dzones:
            if not z:
                continue
            for a in z.find_all("a", href=True):
                abs_href = urljoin(doc_url, a["href"])
                for key, pattern in SOCIAL_PATTERNS.items():
                    if key in ("documentURL",):
                        continue
                    if not links.get(key) and pattern.search(abs_href):
                        links[key] = abs_href

    # если главная пустая целиком - fallback на browser_fetch.js как было
    if is_main_page and all(not links[k] for k in links if k != "websiteURL"):
        logger.info(
            "Обычный парс html (requests + BeautifulSoup): пусто на %s - повтор через Playwright",
            base_url,
        )
        browser_out = fetch_url_html_playwright(base_url)
        try:
            j2 = json.loads(browser_out)
            if isinstance(j2, dict) and "websiteURL" in j2:
                logger.info("Соцлинки (fallback browser): %s", j2)
                for k, v in list(j2.items()):
                    if isinstance(v, str):
                        j2[k] = force_https(v)
                return j2
        except Exception as e:
            logger.warning("extract_social_links fallback JSON error: %s", e)

    # Финальная нормализация - все в https
    for k, v in list(links.items()):
        if v and isinstance(v, str):
            links[k] = force_https(v)

    return links


# Сбор внутренних ссылок сайта (ограничение max_links), с кэшем
def get_internal_links(html: str, base_url: str, max_links: int = 10) -> list[str]:
    if base_url in PARSED_INTERNALS_CACHE:
        return PARSED_INTERNALS_CACHE[base_url]

    # если json от браузера - пропуск
    if _looks_like_browser_json(html):
        PARSED_INTERNALS_CACHE[base_url] = []
        logger.debug("Внутренние ссылки для %s: пропущено (browser JSON)", base_url)
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    found: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href.startswith(base_url) and href not in found:
            found.add(href)
            if len(found) >= max_links:
                break

    PARSED_INTERNALS_CACHE[base_url] = []
    logger.debug("Внутренние ссылки для %s: пропуск (стратегия main+docs)", base_url)
    return []


# Экспорт
__all__ = [
    "fetch_url_html",
    "fetch_url_html_playwright",
    "extract_social_links",
    "find_best_docs_link",
    "get_internal_links",
    "is_html_suspicious",
    "has_social_links",
    "get_domain_name",
    "extract_project_name",
]
