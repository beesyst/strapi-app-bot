import copy
import json
import json as _json
import os
import re
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger

# Логгер
logger = get_logger("web_parser")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)
BAD_NAME_KEYWORDS = set(map(str.lower, CONFIG.get("bad_name_keywords", [])))

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


def is_bad_name(name):
    name = (name or "").strip().lower()
    if not name or len(name) < 3:
        return True
    for bad_kw in BAD_NAME_KEYWORDS:
        if not bad_kw:
            continue
        if name == bad_kw or bad_kw in name:
            return True
    if len(name.split()) > 3:
        return True
    return False


# HTML через browser_fetch.js (Playwright + Fingerprint-suite)
def fetch_url_html_playwright(url):
    script_path = os.path.join(ROOT_DIR, "core", "browser_fetch.js")
    try:
        result = subprocess.run(
            ["node", script_path, url],
            cwd=os.path.dirname(script_path),
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode == 0:
            logger.info("Соцлинки получены через browser_fetch.js: %s", url)
            return result.stdout
        else:
            logger.warning("browser_fetch.js error for %s: %s", url, result.stderr)
            return "{}"
    except Exception as e:
        logger.warning("Ошибка запуска browser_fetch.js для %s: %s", url, e)
        return "{}"


# Очистка и нормализация имени проекта
def clean_project_name(name):
    name = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", name)
    name = re.sub(r"[^A-Za-zА-Яа-я0-9\- ]", "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" ,.")
    return name.strip()


# "Подозрительный" html
def is_html_suspicious(html):
    if (
        "cf-browser-verification" in html
        or "Cloudflare" in html
        or "Just a moment..." in html
    ):
        return True
    if len(html) < 2500:
        return True
    for dom in [
        "twitter.com",
        "x.com",
        "discord.gg",
        "t.me",
        "telegram.me",
        "github.com",
        "medium.com",
    ]:
        if dom in html:
            return False
    return True


def has_social_links(html):
    for dom in [
        "twitter.com",
        "x.com",
        "discord.gg",
        "t.me",
        "telegram.me",
        "github.com",
        "medium.com",
    ]:
        if dom in html:
            return True
    return False


# Загрузка HTML страницы с кэшем, лог успех/ошибка
def fetch_url_html(url):
    if url in FETCHED_HTML_CACHE:
        return FETCHED_HTML_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(url, headers=headers, timeout=30).text
        FETCHED_HTML_CACHE[url] = html
        return html
    except Exception as e:
        logger.warning("Ошибка получения HTML %s: %s", url, e)
        html = fetch_url_html_playwright(url)
        FETCHED_HTML_CACHE[url] = html
        return html


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


# Преобразование youtube-ссылки
def youtube_to_handle(url: str) -> str:
    u = force_https(url)
    if not u or not isinstance(u, str):
        return u

    # @handle
    if re.search(r"^https://(www\.)?youtube\.com/@[A-Za-z0-9_.-]+/?$", u, re.I):
        return u

    # Поддержка youtube.com и youtu.be
    if not (("youtube.com" in u) or ("youtu.be" in u)):
        return u

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(u, headers=headers, timeout=10, allow_redirects=True)
        final_url = force_https(resp.url or u)
        html = resp.text or ""

        # Если редирект на @handle
        m_final = re.search(
            r"https://(www\.)?youtube\.com/(@[A-Za-z0-9_.-]+)", final_url, re.I
        )
        if m_final:
            return f"https://www.youtube.com/{m_final.group(2)}"

        # Канонический URL в <link rel="canonical" ...>
        m_canon = re.search(
            r'rel=["\']canonical["\']\s+href=["\'](https?://(www\.)?youtube\.com/(@[A-Za-z0-9_.-]+))',
            html,
            re.I,
        )
        if m_canon:
            return force_https(m_canon.group(1))

        # canonicalBaseUrl в встраиваемых скриптах
        m_base = re.search(r'"canonicalBaseUrl":"\\?/(@[A-Za-z0-9_.-]+)"', html)
        if m_base:
            return f"https://www.youtube.com/{m_base.group(1)}"

        # og:url тоже часто содержит @handle
        m_og = re.search(
            r'property=["\']og:url["\']\s+content=["\'](https?://(www\.)?youtube\.com/(@[A-Za-z0-9_.-]+))',
            html,
            re.I,
        )
        if m_og:
            return force_https(m_og.group(1))

        # Спец-кейс для /channel/ID: попытка извлечь @handle из HTML
        if re.match(
            r"^https://(www\.)?youtube\.com/channel/[A-Za-z0-9_\-]+", final_url, re.I
        ):
            return final_url

        # Прочие форматы (/c/..., /user/..., /citi)
        return final_url
    except Exception as e:
        logger.warning("youtube_to_handle error for %s: %s", u, e)
        return u


# Хелпер принудительного перевода http в https
def force_https(url: str) -> str:
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Привод соцссылок к единому виду
def normalize_socials(socials):
    # Twitter -> X
    if socials.get("twitterURL"):
        socials["twitterURL"] = socials["twitterURL"].replace("twitter.com", "x.com")
    # YouTube -> @handle (универсально: /c, /user, /channel, произвольные пути)
    if socials.get("youtubeURL"):
        socials["youtubeURL"] = youtube_to_handle(socials["youtubeURL"])
    # Принудительный https для всех URL
    for k, v in list(socials.items()):
        if not v:
            continue
        socials[k] = force_https(v)
    return socials


# Видеоролики на главной
def extract_youtube_featured_videos(channel_handle_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        html = requests.get(channel_handle_url, headers=headers, timeout=10).text
        m = re.search(r"ytInitialData\s*=\s*(\{.*?\});", html, re.DOTALL)
        if not m:
            logger.warning("ytInitialData не найден на %s", channel_handle_url)
            return []
        data = _json.loads(m.group(1))
        featured = []

        try:
            tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
            for tab in tabs:
                tabRenderer = tab.get("tabRenderer")
                if not tabRenderer or not tabRenderer.get("selected"):
                    continue
                content = tabRenderer.get("content", {})
                sectionList = content.get("sectionListRenderer", {}).get("contents", [])
                for section in sectionList:
                    items = section.get("itemSectionRenderer", {}).get("contents", [])
                    for item in items:
                        player = item.get("channelVideoPlayerRenderer")
                        if player:
                            video_id = player.get("videoId")
                            title_obj = player.get("title", {})
                            if "runs" in title_obj and title_obj["runs"]:
                                title = title_obj["runs"][0]["text"]
                            else:
                                title = title_obj.get("simpleText", "")
                            featured.append({"videoId": video_id, "title": title})
        except Exception as e:
            logger.warning("Ошибка парсинга JSON YouTube: %s", e)
            return []
        return featured
    except Exception as e:
        logger.warning("Ошибка запроса/parsing YouTube: %s", e)
        return []


# Хелпер Youtube
def youtube_watch_to_embed(url: str) -> str:
    u = force_https(url or "")
    # youtu.be/ID -> embed/ID
    m = re.search(r"https?://(?:www\.)?youtu\.be/([A-Za-z0-9_-]{6,})", u, re.I)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"
    # youtube.com/watch?v=ID -> embed/ID
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"
    return ""


# title через oEmbed; fallback — og:title из HTML
def youtube_oembed_title(url: str) -> str:
    o = ""
    try:
        oembed = f"https://www.youtube.com/oembed?url={requests.utils.quote(url, safe='')}&format=json"
        r = requests.get(oembed, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            o = (r.json() or {}).get("title", "") or ""
    except Exception:
        pass
    if o:
        return o
    # fallback: og:title из HTML
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        m = re.search(
            r'property=["\']og:title["\']\s+content=["\']([^"\']+)', r.text or "", re.I
        )
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


# Доменное имя из URL и лог результата
def get_domain_name(url):
    domain = urlparse(url).netloc
    result = domain.replace("www.", "").split(".")[0]
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
            timeout=90,
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
    # Парс JSON из browser_fetch.js
    try:
        links = json.loads(html)
        if isinstance(links, dict) and "websiteURL" in links:
            logger.info("Соцлинки из browser_fetch.js: %s", links)
            return links
    except Exception:
        pass

    # Обычный HTML-парсинг через BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_href = urljoin(base_url, href)
        for key, pattern in SOCIAL_PATTERNS.items():
            if key == "documentURL":
                continue
            if pattern.search(abs_href):
                links[key] = abs_href
    links["websiteURL"] = base_url
    document_url = find_best_docs_link(soup, base_url)
    if document_url:
        links["documentURL"] = document_url
    else:
        links["documentURL"] = ""

    if is_main_page and all(not links[k] for k in links if k != "websiteURL"):
        logger.info(
            f"extract_social_links: ни одной соцссылки не найдено для {base_url}, повтор через browser_fetch.js"
        )
        html_browser = fetch_url_html_playwright(base_url)
        try:
            browser_links = json.loads(html_browser)
            if isinstance(browser_links, dict) and "websiteURL" in browser_links:
                logger.info(
                    "Соцлинки из browser_fetch.js (fallback): %s", browser_links
                )
                return browser_links
        except Exception as e:
            logger.warning(f"extract_social_links fallback JSON error: {e}")
    return links


# Основная функция для сбора соцсетей и docs по проекту
def collect_main_data(url, main_template, storage_path=None, max_internal_links=10):
    main_data = copy.deepcopy(main_template)
    found_socials = {}
    html = fetch_url_html(url)
    socials = extract_social_links(html, url, is_main_page=True)
    found_socials.update({k: v for k, v in socials.items() if v})
    logger.info(f"Соцлинков найдено: {found_socials}")

    # Сбор внутренних соцлинков
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

    # Docs поиск
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
    # Fallback docs
    if not found_socials.get("documentURL"):
        domain = urlparse(url).netloc
        domain_root = domain.replace("www.", "")
        candidate_docs = [
            f"https://docs.{domain_root}/",
            f"https://{domain_root}/docs",
            f"https://{domain_root}/docs/",
            f"https://{domain_root}/documentation",
            f"https://{domain_root}/whitepaper",
        ]
        for docs_url in candidate_docs:
            try:
                resp = requests.get(
                    docs_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"}
                )
                html = resp.text
                if resp.status_code == 200 and len(html) > 2200:
                    soup = BeautifulSoup(html, "html.parser")
                    title = (soup.title.string if soup.title else "").lower()
                    doc_kw = (
                        "doc" in title
                        or "documentation" in title
                        or "sidebar" in html
                        or "menu" in html
                        or "docs" in docs_url
                    )
                    if doc_kw:
                        logger.info("Авто-найдена docs-ссылка: %s", docs_url)
                        found_socials["documentURL"] = docs_url
                        break
            except Exception as e:
                logger.warning("Ошибка автопоиска docs (%s): %s", docs_url, e)

    # Video Slider
    main_data["videoSlider"] = []
    youtube_url = found_socials.get("youtubeURL", "")
    if youtube_url:
        youtube_url = youtube_to_handle(youtube_url)


    def _pack_slide(url: str, title: str = "") -> dict:
        url = force_https(url or "")
        title = title or youtube_oembed_title(url)
        embed = youtube_watch_to_embed(url)
        payload = {"url": url, "title": title or "", "embed": embed or ""}
        return {"video": json.dumps(payload, ensure_ascii=False)}


    if youtube_url and (
        "youtube.com/@" in youtube_url or "/channel/" in youtube_url or "/c/" in youtube_url
    ):
        try:
            yt_json = fetch_url_html_playwright(youtube_url)
            logger.info(f"Youtube-url: {youtube_url}")
            yt_links = json.loads(yt_json)
            fv = yt_links.get("featuredVideos") or []
            if fv:
                main_data["videoSlider"] = [
                    _pack_slide(v.get("url", ""), v.get("title", ""))
                    for v in fv
                    if v.get("url")
                ]
        except Exception as e:
            logger.warning(f"Ошибка парса featuredVideos через browser_fetch.js: {e}")

    # Twitter: аватар и имя
    avatar_url = ""
    twitter_name = ""
    if found_socials.get("twitterURL"):
        twitter_result = get_links_from_x_profile(found_socials["twitterURL"])
        bio_links = twitter_result.get("links", [])
        avatar_url = twitter_result.get("avatar", "")
        raw_twitter_name = twitter_result.get("name", "")
        twitter_name = re.split(r"\||-", (raw_twitter_name or "").strip())[0].strip()
        for bio_url in bio_links:
            for k in main_template["socialLinks"].keys():
                if k.endswith("URL") and not found_socials.get(k):
                    if bio_url.lower().startswith(k.replace("URL", "").lower()):
                        found_socials[k] = bio_url

    # Имя из title
    soup = BeautifulSoup(html, "html.parser")
    raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    title_name = re.split(r"\||-", raw_title)[0].strip()

    clean_twitter = clean_project_name(twitter_name)
    clean_title = clean_project_name(title_name)
    if clean_twitter and clean_title and clean_twitter.lower() == clean_title.lower():
        project_name = clean_twitter
    elif clean_twitter and not is_bad_name(clean_twitter):
        project_name = clean_twitter
    elif clean_title and not is_bad_name(clean_title):
        project_name = clean_title
    else:
        project_name = get_domain_name(url).capitalize()

    # Fail-safe
    if (
        not project_name
        or not isinstance(project_name, str)
        or not project_name.strip()
    ):
        project_name = get_domain_name(url).capitalize()
    main_data["name"] = project_name

    logger.info("Итоговое имя проекта: '%s'", project_name)

    # Аватар
    logo_filename = f"{project_name.lower().replace(' ', '')}.jpg"
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

    # Финальная сборка main_data
    social_keys = list(main_template["socialLinks"].keys())
    final_socials = {k: found_socials.get(k, "") for k in social_keys}
    final_socials = normalize_socials(final_socials)
    main_data["socialLinks"] = final_socials
    return main_data
