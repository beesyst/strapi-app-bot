import os
import re
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Логирование в host.log
from core.log_utils import log_info, log_warning

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
        r"^https?://(?!twitter|x|discord|t\.me|youtube|linkedin|reddit|medium|github)",
        re.I,
    ),
    "documentURL": re.compile(r"docs\.", re.I),
}


# Получение html по url (user-agent для обхода блокировок)
def fetch_url_html(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(url, headers=headers, timeout=10).text
        log_info(f"[web_parser] HTML успешно получен: {url}")
        return html
    except Exception as e:
        log_warning(f"[web_parser] Ошибка получения HTML {url}: {e}")
        return ""


# Поиск внутренних ссылок
def get_internal_links(html, base_url, max_links=10):
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        # Только ссылки внутри этого сайта
        if href.startswith(base_url) and href not in found:
            found.add(href)
            if len(found) >= max_links:
                break
    log_info(f"[web_parser] Внутренние ссылки для {base_url}: {list(found)}")
    return list(found)


# Нормализация ссылок (например, twitter.com → x.com)
def normalize_socials(socials):
    if socials.get("twitterURL"):
        socials["twitterURL"] = socials["twitterURL"].replace("twitter.com", "x.com")
    return socials


# Получение домена из url (например, https://site.com/abc → site)
def get_domain_name(url):
    domain = urlparse(url).netloc
    result = domain.replace("www.", "").split(".")[0]
    log_info(f"[web_parser] get_domain_name({url}) -> {result}")
    return result


# Вызов Node.js-парсера для Twitter/X профиля (twitter_parser.js)
def get_links_from_x_profile(profile_url):
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
    script_path = os.path.join(NODE_CORE_DIR, "twitter_parser.js")
    try:
        result = subprocess.run(
            ["node", script_path, profile_url],
            cwd=NODE_CORE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            log_info(f"[web_parser] twitter_parser.js успешно обработал: {profile_url}")
            return safe_json_loads(result.stdout)
        else:
            log_warning(
                f"[web_parser] twitter_parser.js error for {profile_url}: {result.stderr}"
            )
            return {"links": [], "avatar": ""}
    except Exception as e:
        log_warning(
            f"[web_parser] Ошибка запуска twitter_parser.js для {profile_url}: {e}"
        )
        return {"links": [], "avatar": ""}


# Helper для тихой загрузки json без падения
def safe_json_loads(data):
    import json

    try:
        return json.loads(data)
    except Exception as e:
        log_warning(f"[web_parser] Ошибка при safe_json_loads: {e}")
        return {}


# Вспомогательная: поиск лучшей docs-ссылки на странице
def find_best_docs_link(soup, base_url):
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.text or "").strip().lower()
        href_full = urljoin(base_url, href)
        if any(
            keyword in text
            for keyword in ["docs", "documentation", "developer docs", "developers"]
        ):
            candidates.append((text, href_full))
    # Фильтруем только "docs", но без api-docs
    filtered = [
        (text, href)
        for text, href in candidates
        if not any(
            skip in href
            for skip in [
                "api-docs",
                "developer-docs",
                "apidocs",
                "api/",
                "api.",
                "developers",
            ]
        )
    ]

    # Сортировка по приоритету
    def score(href):
        parsed = urlparse(href)
        if re.match(r".*/docs/?$", parsed.path) and not parsed.netloc.startswith(
            "api."
        ):
            return 0  # domain.com/docs
        if parsed.netloc.startswith("docs."):
            return 1  # docs.domain.com
        return 2

    if filtered:
        filtered.sort(key=lambda t: score(t[1]))
        doc_url = filtered[0][1]
        log_info(f"[web_parser] Лучшая docs-ссылка найдена: {doc_url}")
        return doc_url
    # fallback: ищем хоть что-то типа docs.domain.com или /docs
    all_hrefs = [urljoin(base_url, a["href"]) for a in soup.find_all("a", href=True)]
    for href in all_hrefs:
        parsed = urlparse(href)
        if re.match(r".*/docs/?$", parsed.path) or parsed.netloc.startswith("docs."):
            log_info(f"[web_parser] Docs fallback-ссылка найдена: {href}")
            return href
    log_info(f"[web_parser] Docs-ссылки не найдены для {base_url}")
    return ""


# Основная функция: вытащить соц.ссылки и docs из html
def extract_social_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}
    # Находим все соцсети
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for key, pattern in SOCIAL_PATTERNS.items():
            if key == "documentURL":
                continue
            if pattern.search(href):
                links[key] = href
    links["websiteURL"] = base_url
    # docs-ссылка отдельным методом
    document_url = find_best_docs_link(soup, base_url)
    if document_url:
        links["documentURL"] = document_url
    else:
        links["documentURL"] = ""
    log_info(f"[web_parser] Соцсети и docs для {base_url}: {links}")
    return links
