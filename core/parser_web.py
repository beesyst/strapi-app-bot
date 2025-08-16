from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger

# Логгер
logger = get_logger("parser_web")

# Пути/конфиг
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
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
    # Короткий HTML
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


# Обертка поверх browser_fetch.js
def fetch_url_html_playwright(url: str, timeout: int = 90) -> str:
    script_path = os.path.join(ROOT_DIR, "core", "browser_fetch.js")
    try:
        result = subprocess.run(
            ["node", script_path, url],
            cwd=os.path.dirname(script_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            logger.info("browser_fetch.js ок: %s", url)
            return result.stdout or ""
        logger.warning(
            "browser_fetch.js error for %s: %s", url, (result.stderr or "").strip()
        )
        return result.stdout or result.stderr or ""
    except Exception as e:
        logger.warning("Запуск browser_fetch.js упал для %s: %s", url, e)
        return ""


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
            logger.info("Лучшая docs-ссылка найдена: %s", doc_url)
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
                return j
    except Exception:
        pass

    # обычный html → парс соцссылок и docs
    soup = BeautifulSoup(html or "", "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}

    for a in soup.find_all("a", href=True):
        abs_href = urljoin(base_url, a["href"])
        for key, pattern in SOCIAL_PATTERNS.items():
            if key == "documentURL":
                continue
            if pattern.search(abs_href):
                links[key] = abs_href

    links["websiteURL"] = base_url
    doc_url = find_best_docs_link(soup, base_url)
    links["documentURL"] = doc_url or ""

    # браузер
    if is_main_page and all(not links[k] for k in links if k != "websiteURL"):
        logger.info(
            "extract_social_links: пусто на %s — повтор через browser_fetch.js",
            base_url,
        )
        browser_out = fetch_url_html_playwright(base_url)
        try:
            j2 = json.loads(browser_out)
            if isinstance(j2, dict) and "websiteURL" in j2:
                logger.info("Соцлинки (fallback browser): %s", j2)
                return j2
        except Exception as e:
            logger.warning("extract_social_links fallback JSON error: %s", e)

    return links


# Сбор внутренних ссылок сайта (ограничение max_links), с кэшем
def get_internal_links(html: str, base_url: str, max_links: int = 10) -> list[str]:
    if base_url in PARSED_INTERNALS_CACHE:
        return PARSED_INTERNALS_CACHE[base_url]

    # если json от браузера - пропуск
    if _looks_like_browser_json(html):
        PARSED_INTERNALS_CACHE[base_url] = []
        logger.info("Внутренние ссылки для %s: пропущено (browser JSON)", base_url)
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    found: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href.startswith(base_url) and href not in found:
            found.add(href)
            if len(found) >= max_links:
                break

    result = list(found)
    PARSED_INTERNALS_CACHE[base_url] = result
    logger.info("Внутренние ссылки для %s: %s", base_url, result)
    return result


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
]
