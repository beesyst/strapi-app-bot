import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger

# Логгер
logger = get_logger("web_parser")

# Кэш для ускорения парсинга
FETCHED_HTML_CACHE = {}
PARSED_SOCIALS_CACHE = {}
PARSED_INTERNALS_CACHE = {}
PARSED_DOCS_LINKS_LOGGED = set()

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
BAD_NAMES = {"", "x", "profile", "new to x"}


# Очистка и нормализация имени проекта
def clean_project_name(name):
    name = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", name)
    name = re.sub(r"[^A-Za-zА-Яа-я0-9\- ]", "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" ,.")
    return name.strip()


# Загрузка HTML страницы с кэшем, лог успех/ошибка
def fetch_url_html(url):
    if url in FETCHED_HTML_CACHE:
        return FETCHED_HTML_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(url, headers=headers, timeout=10).text
        FETCHED_HTML_CACHE[url] = html
        logger.info("HTML успешно получен: %s", url)
        return html
    except Exception as e:
        logger.warning("Ошибка получения HTML %s: %s", url, e)
        FETCHED_HTML_CACHE[url] = ""
        return ""


# Поиск внутренних сссылок сайта (максимум max_links) с кэшем
def get_internal_links(html, base_url, max_links=10):
    if base_url in PARSED_INTERNALS_CACHE:
        return PARSED_INTERNALS_CACHE[base_url]
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href.startswith(base_url) and href not in found:
            found.add(href)
            if len(found) >= max_links:
                break
    links_list = list(found)
    PARSED_INTERNALS_CACHE[base_url] = links_list
    logger.info("Внутренние ссылки для %s: %s", base_url, links_list)
    return links_list


# Привод соцссылок к единому виду (например, twitter → x)
def normalize_socials(socials):
    if socials.get("twitterURL"):
        socials["twitterURL"] = socials["twitterURL"].replace("twitter.com", "x.com")
    return socials


# Доменное имя из URL и лог результата
def get_domain_name(url):
    domain = urlparse(url).netloc
    result = domain.replace("www.", "").split(".")[0]
    logger.info("get_domain_name(%s) -> %s", url, result)
    return result


# Ссылки и имя/аватар из X/Twitter через Node-скрипт
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
            logger.info("twitter_parser.js успешно обработал: %s", profile_url)
            return safe_json_loads(result.stdout)
        else:
            logger.warning(
                "twitter_parser.js error for %s: %s", profile_url, result.stderr
            )
            return {"links": [], "avatar": "", "name": ""}
    except Exception as e:
        logger.warning("Ошибка запуска twitter_parser.js для %s: %s", profile_url, e)
        return {"links": [], "avatar": "", "name": ""}


# Безопасный json.loads с логом ошибок
def safe_json_loads(data):
    import json

    try:
        return json.loads(data)
    except Exception as e:
        logger.warning("Ошибка при safe_json_loads: %s", e)
        return {}


# Поиск лучшей ссылки на док среди ссылок страницы
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

    def score(href):
        parsed = urlparse(href)
        if re.match(r".*/docs/?$", parsed.path) and not parsed.netloc.startswith(
            "api."
        ):
            return 0  # domain.com/docs
        if parsed.netloc.startswith("docs."):
            return 1  # docs.domain.com
        return 2

    doc_url = ""
    if filtered:
        filtered.sort(key=lambda t: score(t[1]))
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


# Извлечение всех соц ссылок и docs с html страницы
def extract_social_links(html, base_url, is_main_page=False):
    if base_url in PARSED_SOCIALS_CACHE:
        return PARSED_SOCIALS_CACHE[base_url]
    soup = BeautifulSoup(html, "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for key, pattern in SOCIAL_PATTERNS.items():
            if key == "documentURL":
                continue
            if pattern.search(href):
                links[key] = href
    links["websiteURL"] = base_url
    document_url = find_best_docs_link(soup, base_url)
    if document_url:
        links["documentURL"] = document_url
    else:
        links["documentURL"] = ""
    PARSED_SOCIALS_CACHE[base_url] = links
    if is_main_page:
        logger.info("Соцсети и docs для %s: %s", base_url, links)
    return links


# Основная функция для сбора соцсетей и docs по проекту (главная + внутренние)
def collect_social_links_main(url, main_template, storage_path=None):
    found_socials = {}
    html = fetch_url_html(url)
    socials = extract_social_links(html, url, is_main_page=True)
    found_socials.update({k: v for k, v in socials.items() if v})

    internal_links = get_internal_links(html, url, max_links=10)
    for link in internal_links:
        try:
            page_html = fetch_url_html(link)
            page_socials = extract_social_links(page_html, link)
            for k, v in page_socials.items():
                if v and not found_socials.get(k):
                    found_socials[k] = v
        except Exception as e:
            logger.warning("Ошибка парсинга %s: %s", link, e)

    found_socials = normalize_socials(found_socials)
    if found_socials.get("documentURL"):
        docs_url = found_socials["documentURL"]
        try:
            docs_html = fetch_url_html(docs_url)
            docs_socials = extract_social_links(docs_html, docs_url)
            for k, v in docs_socials.items():
                if v and not found_socials.get(k):
                    found_socials[k] = v
        except Exception as e:
            logger.warning("Ошибка docs: %s", e)

    project_name = ""
    if not found_socials.get("twitterURL"):
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        m = re.match(r"^(.+?)\s*[\(/]", title)
        if m and m.group(1):
            project_name = m.group(1).strip()
            logger.info("Имя проекта по <title> (pattern): '%s'", project_name)
        elif title:
            project_name = title
            logger.info("Имя проекта по <title>: '%s'", project_name)
        else:
            project_name = get_domain_name(url).capitalize()
            logger.info("Имя проекта по домену: '%s'", project_name)
    clean_name = clean_project_name(project_name)
    if not clean_name:
        clean_name = get_domain_name(url).capitalize()
    return found_socials, clean_name


# Получение и скачивание аватар и имя из X/Twitter
def fetch_twitter_avatar_and_name(twitter_url, storage_path, base_name, max_retries=3):
    twitter_result = None
    avatar_url = ""
    raw_name = ""
    for attempt in range(max_retries):
        twitter_result = get_links_from_x_profile(twitter_url)
        avatar_url = twitter_result.get("avatar", "")
        raw_name = twitter_result.get("name", "") or base_name
        logger.info(
            "twitter_parser.js вернул имя: '%s' (попытка %d)", raw_name, attempt + 1
        )
        if (
            not raw_name
            or len(raw_name.strip()) < 3
            or raw_name.strip().lower() in BAD_NAMES
        ):
            logger.warning(
                "Имя из twitter_parser.js ('%s') невалидно, fallback на base_name ('%s')",
                raw_name,
                base_name,
            )
            raw_name = base_name
        if avatar_url or attempt == max_retries - 1:
            break
        time.sleep(2)

    name = clean_project_name(raw_name)
    if not name or len(name) < 3:
        logger.warning(
            "Имя после clean_project_name ('%s') короткое, fallback на base_name ('%s')",
            name,
            base_name,
        )
        name = clean_project_name(base_name)

    logo_filename = f"{name.lower().replace(' ', '')}.jpg"
    avatar_path = ""
    if avatar_url and storage_path:
        for attempt in range(max_retries):
            try:
                avatar_data = requests.get(avatar_url, timeout=10).content
                avatar_path = os.path.join(storage_path, logo_filename)
                with open(avatar_path, "wb") as imgf:
                    imgf.write(avatar_data)
                logger.info("Скачан: %s → %s", avatar_url, avatar_path)
                return logo_filename, name
            except Exception as e:
                logger.warning(
                    "Ошибка скачивания аватара (попытка %d): %s", attempt + 1, e
                )
                time.sleep(1)
        return "", name
    return "", name


# Сбор всех соц ссылок и docs, а также загрузка аватара и обновление main_data
def collect_all_socials(url, main_template, storage_path=None, max_internal_links=10):
    import copy

    main_data = copy.deepcopy(main_template)
    found_socials = {}
    html = fetch_url_html(url)
    socials = extract_social_links(html, url, is_main_page=True)
    found_socials.update({k: v for k, v in socials.items() if v})

    internal_links = get_internal_links(html, url, max_links=max_internal_links)
    for link in internal_links:
        try:
            page_html = fetch_url_html(link)
            page_socials = extract_social_links(page_html, link)
            for k, v in page_socials.items():
                if v and not found_socials.get(k):
                    found_socials[k] = v
        except Exception as e:
            logger.warning("Ошибка парсинга %s: %s", link, e)

    found_socials = normalize_socials(found_socials)
    if found_socials.get("documentURL"):
        docs_url = found_socials["documentURL"]
        try:
            docs_html = fetch_url_html(docs_url)
            docs_socials = extract_social_links(docs_html, docs_url)
            for k, v in docs_socials.items():
                if v and not found_socials.get(k):
                    found_socials[k] = v
        except Exception as e:
            logger.warning("Ошибка docs: %s", e)

    avatar_path = ""
    project_name = ""
    avatar_url = ""
    if found_socials.get("twitterURL"):
        twitter_result = get_links_from_x_profile(found_socials["twitterURL"])
        bio_links = twitter_result.get("links", [])
        avatar_url = twitter_result.get("avatar", "")
        project_name = twitter_result.get("name", "")

        for bio_url in bio_links:
            for k in main_template["socialLinks"].keys():
                if k.endswith("URL") and not found_socials.get(k):
                    if bio_url.lower().startswith(k.replace("URL", "").lower()):
                        found_socials[k] = bio_url

    def is_bad_name(name):
        name = (name or "").strip()
        return (
            not name
            or len(name) < 3
            or name.lower() in BAD_NAMES
            or name.lower() == "twitter"
        )

    if is_bad_name(project_name):
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        m = re.match(r"^(.+?)\s*[\(/]", title)
        if m and m.group(1) and not is_bad_name(m.group(1)):
            project_name = m.group(1).strip()
        elif title and not is_bad_name(title):
            project_name = title
        else:
            project_name = get_domain_name(url).capitalize()
    clean_name = clean_project_name(project_name)
    if not clean_name:
        clean_name = get_domain_name(url).capitalize()
    logger.info("Итоговое имя проекта: '%s'", clean_name)

    logo_filename = f"{clean_name.lower().replace(' ', '')}.jpg"
    if found_socials.get("twitterURL") and avatar_url and storage_path:
        avatar_path = os.path.join(storage_path, logo_filename)
        try:
            avatar_data = requests.get(avatar_url, timeout=10).content
            with open(avatar_path, "wb") as imgf:
                imgf.write(avatar_data)
            main_data["svgLogo"] = logo_filename
        except Exception as e:
            logger.warning("Ошибка скачивания аватара: %s", e)
    else:
        main_data["svgLogo"] = logo_filename
    logger.info(
        "Итоговый svgLogo: %s (avatar_url: %s)", main_data["svgLogo"], avatar_url
    )
    social_keys = list(main_template["socialLinks"].keys())
    final_socials = {k: found_socials.get(k, "") for k in social_keys}
    main_data["socialLinks"] = final_socials
    main_data["name"] = clean_name
    return main_data, avatar_path
