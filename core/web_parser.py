import os
import re
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Логирование
from core.log_utils import log_info, log_warning

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


# Очистка имени проекта (убирает мусор и скобки)
def clean_project_name(name):
    name = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", name)
    name = re.sub(r"[^A-Za-zА-Яа-я0-9\- ]", "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" ,.")
    return name.strip()


# Получение html по url (user-agent для обхода блокировок)
def fetch_url_html(url):
    if url in FETCHED_HTML_CACHE:
        return FETCHED_HTML_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(url, headers=headers, timeout=10).text
        FETCHED_HTML_CACHE[url] = html
        log_info(f"[web_parser] HTML успешно получен: {url}")
        return html
    except Exception as e:
        log_warning(f"[web_parser] Ошибка получения HTML {url}: {e}")
        FETCHED_HTML_CACHE[url] = ""
        return ""


# Поиск внутренних ссылок (до max_links)
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
    log_info(f"[web_parser] Внутренние ссылки для {base_url}: {links_list}")
    return links_list


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
            return {"links": [], "avatar": "", "name": ""}
    except Exception as e:
        log_warning(
            f"[web_parser] Ошибка запуска twitter_parser.js для {profile_url}: {e}"
        )
        return {"links": [], "avatar": "", "name": ""}


# Безопасная загрузка JSON
def safe_json_loads(data):
    import json

    try:
        return json.loads(data)
    except Exception as e:
        log_warning(f"[web_parser] Ошибка при safe_json_loads: {e}")
        return {}


# Поиск лучшей docs-ссылки на странице
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

    doc_url = ""
    if filtered:
        filtered.sort(key=lambda t: score(t[1]))
        doc_url = filtered[0][1]
    else:
        # fallback: ищем хоть что-то типа docs.domain.com или /docs
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
        # log only once per docs_url
        if doc_url not in PARSED_DOCS_LINKS_LOGGED:
            log_info(f"[web_parser] Лучшая docs-ссылка найдена: {doc_url}")
            PARSED_DOCS_LINKS_LOGGED.add(doc_url)
        return doc_url
    return ""


# Вытащить соц.ссылки и docs из html
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
        log_info(f"[web_parser] Соцсети и docs для {base_url}: {links}")
    return links


# Только соцсети/docs/имя (без аватара) - для async сбора
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
            log_warning(f"[web_parser] Ошибка парсинга {link}: {e}")

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
            log_warning(f"[web_parser] Ошибка docs: {e}")

    # Имя проекта: twitter даст имя позже, если есть, иначе <title>
    project_name = ""
    if not found_socials.get("twitterURL"):
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        m = re.match(r"^(.+?)\s*[\(/]", title)
        if m and m.group(1):
            project_name = m.group(1).strip()
        elif title:
            project_name = title
        else:
            project_name = get_domain_name(url).capitalize()
    clean_name = clean_project_name(project_name)
    if not clean_name:
        clean_name = get_domain_name(url).capitalize()
    return found_socials, clean_name


# Получить аватар и имя из твиттера (sync)
def fetch_twitter_avatar_and_name(twitter_url, storage_path, base_name):
    twitter_result = get_links_from_x_profile(twitter_url)
    avatar_url = twitter_result.get("avatar", "")
    raw_name = twitter_result.get("name", "") or base_name
    name = clean_project_name(raw_name)
    logo_filename = f"{name.lower().replace(' ', '')}.jpg"
    avatar_path = ""
    if avatar_url and storage_path:
        avatar_path = os.path.join(storage_path, logo_filename)
        try:
            avatar_data = requests.get(avatar_url, timeout=10).content
            with open(avatar_path, "wb") as imgf:
                imgf.write(avatar_data)
            log_info(f"[web_parser] Скачан: {avatar_url} → {avatar_path}")
            return logo_filename, name
        except Exception as e:
            log_warning(f"[web_parser] Ошибка скачивания аватара: {e}")
            return "", name
    return "", name


# Сбор всех соцсетей + docs + имя + аватар
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
            log_warning(f"[web_parser] Ошибка парсинга {link}: {e}")

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
            log_warning(f"[web_parser] Ошибка docs: {e}")

    avatar_path = ""
    project_name = ""
    avatar_url = ""
    # Извлекаем имя и аватар из твиттера, а также bio-ссылки
    if found_socials.get("twitterURL"):
        twitter_result = get_links_from_x_profile(found_socials["twitterURL"])
        bio_links = twitter_result.get("links", [])
        avatar_url = twitter_result.get("avatar", "")
        project_name = twitter_result.get("name", "")

        # bio-ссылки из профиля
        for bio_url in bio_links:
            for k in main_template["socialLinks"].keys():
                if k.endswith("URL") and not found_socials.get(k):
                    if bio_url.lower().startswith(k.replace("URL", "").lower()):
                        found_socials[k] = bio_url

    # Проверка: плохое ли имя (короткое, X, мусор)
    def is_bad_name(name):
        name = (name or "").strip()
        return not name or len(name) < 3 or name.lower() in {"x", "twitter", "profile"}

    # Fallback - <title> страницы, если имя плохое
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
    # Очистка имени
    clean_name = clean_project_name(project_name)
    if not clean_name:
        clean_name = get_domain_name(url).capitalize()
    log_info(f"[web_parser] Итоговое имя проекта: '{clean_name}'")

    # Формируем svgLogo из чистого имени
    logo_filename = f"{clean_name.lower().replace(' ', '')}.jpg"
    if found_socials.get("twitterURL") and avatar_url and storage_path:
        avatar_path = os.path.join(storage_path, logo_filename)
        try:
            avatar_data = requests.get(avatar_url, timeout=10).content
            with open(avatar_path, "wb") as imgf:
                imgf.write(avatar_data)
            main_data["svgLogo"] = logo_filename
        except Exception as e:
            log_warning(f"[web_parser] Ошибка скачивания аватара: {e}")
    else:
        main_data["svgLogo"] = logo_filename
    log_info(
        f"[web_parser] Итоговый svgLogo: {main_data['svgLogo']} (avatar_url: {avatar_url})"
    )
    # Собираем итоговые соцсети
    social_keys = list(main_template["socialLinks"].keys())
    final_socials = {k: found_socials.get(k, "") for k in social_keys}
    main_data["socialLinks"] = final_socials
    # Имя только чистое
    main_data["name"] = clean_name
    return main_data, avatar_path
