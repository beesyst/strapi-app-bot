import copy
import json
import json as _json
import os
import re
import subprocess
from urllib.parse import quote as urlquote
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
LINK_COLLECTION_DOMAINS = set(
    (d or "").lower().replace("www.", "") for d in CONFIG.get("link_collections", [])
)

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
        r"^https?://(?!(?:www\.)?(?:twitter\.com|x\.com|discord\.gg|discord\.com|t\.me|telegram\.me|"
        r"youtube\.com|youtu\.be|linkedin\.com|reddit\.com|medium\.com|github\.com))",
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
    m = re.search(r"https?://(?:www\.)?youtu\.be/([A-Za-z0-9_-]{11})", u, re.I)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"
    # youtube.com/watch?v=ID -> embed/ID
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"
    return ""


# title через oEmbed; fallback — og:title из HTML
def youtube_oembed_title(url: str) -> str:
    o = ""
    try:
        oembed = (
            f"https://www.youtube.com/oembed?url={urlquote(url, safe='')}&format=json"
        )
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


PARSED_X_PROFILE_CACHE = {}


# Первый валидный JSON-объект
def _extract_first_json_object(s: str) -> dict:
    if not s:
        return {}
    start = s.find("{")
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                chunk = s[start : i + 1]
                try:
                    return json.loads(chunk)
                except Exception:
                    break
    return {}


def get_links_from_x_profile(profile_url):
    NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
    script_path = os.path.join(NODE_CORE_DIR, "twitter_parser.js")

    # twitter.com -> x.com + без "/photo"
    orig_url = profile_url.strip()
    safe_url = re.sub(r"^https://twitter\.com", "https://x.com", orig_url, flags=re.I)
    safe_url = force_https(safe_url.rstrip("/"))

    # кэш
    if safe_url in PARSED_X_PROFILE_CACHE:
        return PARSED_X_PROFILE_CACHE[safe_url]

    def _run_once(url_):
        try:
            return subprocess.run(
                ["node", script_path, url_],
                cwd=NODE_CORE_DIR,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except Exception as e:
            logger.warning("Ошибка запуска twitter_parser.js для %s: %s", url_, e)
            return None

    # запуск + 1 ретрай
    tries = [safe_url, safe_url + "/photo"]
    data = {}
    for idx, u in enumerate(tries):
        if u != orig_url:
            logger.info("twitter_parser.js: нормализовал URL %s -> %s", orig_url, u)
        result = _run_once(u)
        if not result:
            continue

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # парс stdout
        data = _extract_first_json_object(stdout)
        if data:
            logger.info("twitter_parser.js успешно обработал: %s", u)
            break

        # в stderr
        data = _extract_first_json_object(stderr)
        if data:
            logger.info("twitter_parser.js: JSON был в stderr: %s", u)
            break

        # лог ошибки для отладки
        logger.warning(
            "twitter_parser.js error for %s: %s", u, (stderr or "no-stderr")[:300]
        )

    if not isinstance(data, dict):
        data = {}
    # нормализованный ответ
    cleaned = {
        "links": data.get("links") or [],
        "avatar": data.get("avatar") or "",
        "name": data.get("name") or "",
    }
    if not cleaned["avatar"] and not cleaned["links"] and not cleaned["name"]:
        logger.warning("twitter_parser.js: пустой результат для %s", safe_url)

    PARSED_X_PROFILE_CACHE[safe_url] = cleaned
    return cleaned


# Нормализация размера авы
def normalize_twitter_avatar(url: str) -> str:
    u = force_https(url or "")
    # _normal/_bigger/_mini/_NxN -> _400x400
    u = re.sub(
        r"(_normal|_bigger|_mini|_\d{2,4}x\d{2,4})\.(jpg|jpeg|png|webp)(\?.*)?$",
        r"_400x400.\2",
        u,
        flags=re.I,
    )
    # Любой name=* -> name=large
    if "name=" in u:
        u = re.sub(r"(\bname=)[a-zA-Z0-9_]+", r"\1large", u)
    else:
        u = re.sub(
            r"(\.(?:jpg|jpeg|png|webp))(?:\?[^#]*)?(#.*)?$",
            r"\1?name=large\2",
            u,
            flags=re.I,
        )
    return u


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


# Все корневые профили X/Twitter со страницы (без /status, /share и т.п.).
def _extract_twitter_profiles(html, base_url):
    soup = BeautifulSoup(html or "", "html.parser")
    profiles = set()

    # Ссылки из <a>
    for a in soup.find_all("a", href=True):
        raw = urljoin(base_url, a["href"])
        if not re.search(r"(twitter\.com|x\.com)", raw, re.I):
            continue
        if re.search(r"/status/|/share|/intent|/search|/hashtag/", raw, re.I):
            continue
        try:
            p = urlparse(raw)
            clean = f"{p.scheme}://{p.netloc}{p.path}"
            clean = clean.replace("twitter.com", "x.com")
            clean = force_https(clean.rstrip("/"))
        except Exception:
            continue
        m = re.match(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})$", clean, re.I)
        if m:
            profiles.add(clean)

    # Ссылки, "зашитые" в текст (инлайновые пропсы/JSON и т.п.)
    text = html or ""
    # Все вхождения x.com/<handle> или twitter.com/<handle> без "хвоста"
    for m in re.finditer(
        r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_/])",
        text,
        re.I,
    ):
        try:
            u = m.group(0)
            u = u.replace("twitter.com", "x.com")
            u = force_https(u.rstrip("/"))
            if re.match(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})$", u, re.I):
                profiles.add(u)
        except Exception:
            pass

    return list(profiles)


def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _is_valid_x_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    name = (parsed.get("name") or "").strip().lower()
    avatar = (parsed.get("avatar") or "").strip()
    links = parsed.get("links") or []
    if (not avatar) and (not name or "new to x" in name) and (not links):
        return False
    return True


# Универсальный парс всех соцлинков со страницы (включая linktr.ee/bento.me/link3.to)
def _extract_socials_from_html(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")
    out = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}
    for a in soup.find_all("a", href=True):
        abs_href = urljoin(base_url, a["href"])
        for key, pattern in SOCIAL_PATTERNS.items():
            if key == "documentURL":
                continue
            if pattern.search(abs_href):
                out[key] = force_https(abs_href)
    out["websiteURL"] = force_https(base_url)
    return out


def _verify_twitter_and_enrich(twitter_url: str, site_domain: str) -> tuple[bool, dict]:
    data = get_links_from_x_profile(twitter_url)
    if not _is_valid_x_profile(data):
        return False, {}

    bio_links = data.get("links") or []
    # прямое подтверждение доменом
    if any(_host(b).endswith(site_domain) for b in bio_links):
        return True, {}

    # подтверждение через линк‑агрегатор + обогащение соцсетей
    aggregator_hosts = LINK_COLLECTION_DOMAINS
    for b in bio_links:
        if _host(b) in aggregator_hosts:
            try:
                agg_html = fetch_url_html(b)
                agg_socials = _extract_socials_from_html(agg_html, b)
                # подтверждение домена через агрегатор
                if any(
                    _host(v).endswith(site_domain) for v in agg_socials.values() if v
                ):
                    # возвращаем все найденные соцссылки для обогащения
                    return True, {k: v for k, v in agg_socials.items() if v}
            except Exception:
                pass

    return False, {}


# Все ссылки на link-агрегаторы
def _extract_link_collection_urls(html, base_url):
    soup = BeautifulSoup(html or "", "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        try:
            host = urlparse(href).netloc.lower().replace("www.", "")
        except Exception:
            continue
        if any(host == d or host.endswith("." + d) for d in LINK_COLLECTION_DOMAINS):
            urls.append(force_https(href))
    return urls


# Догадки хэндлов X по бренду (<=15 символов, валидный набор)
def _guess_twitter_handles(brand_token: str):
    bt = (brand_token or "").strip().lower()
    if not bt:
        return []
    variants = [
        bt,
        f"{bt}data",
        f"{bt}capital",
        f"get{bt}",
        f"{bt}hq",
        f"{bt}protocol",
        f"{bt}labs",
        f"{bt}ai",
        f"{bt}finance",
    ]
    # валидные хэндлы X: буквы/цифры/подчеркивание, длина 1..15
    clean = []
    for v in variants:
        v = re.sub(r"[^a-z0-9_]", "", v)
        if 1 <= len(v) <= 15:
            clean.append(v)
    # сохранине порядка и уникальность
    seen, out = set(), []
    for h in clean:
        if h not in seen:
            out.append(h)
            seen.add(h)
    return out


# Вытаскивание всех X-профилей
def _fetch_and_extract_twitter_profiles(from_url):
    try:
        html = fetch_url_html(from_url)
        return _extract_twitter_profiles(html, from_url)
    except Exception:
        return []


# Проверка валидности данных X-профиля
def _is_valid_x_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    name = (parsed.get("name") or "").strip().lower()
    avatar = (parsed.get("avatar") or "").strip()
    links = parsed.get("links") or []
    if (not avatar) and (not name or "new to x" in name) and (not links):
        return False
    return True


# Выбор лучшего twitter
def _pick_best_twitter(
    current_url,
    candidates,
    brand_token,
    site_domain,
    max_profile_checks=None,
    observed_handles=None,
):
    observed_handles = set(observed_handles or [])

    def score(handle, bio_has_domain=False, is_observed=False):
        s = 0
        # базовый приоритет за совпадение бренда
        if brand_token and brand_token in (handle or "").lower():
            s += 100
        # ссылка на домен в био - сильный сигнал
        if bio_has_domain:
            s += 80
        # найден на сайте/доксах/агрегаторе - повышение доверия
        if is_observed:
            s += 40
        # начинается с brand_token - небольшой бонус
        if brand_token and (handle or "").lower().startswith(brand_token.lower()):
            s += 20
        return s

    seen = set()
    pool = []
    for u in candidates:
        if u not in seen:
            pool.append(u)
            seen.add(u)
    if current_url and current_url not in seen:
        pool.append(current_url)

    if not pool:
        return current_url

    limit = len(pool) if max_profile_checks is None else max_profile_checks

    best = (current_url or "", -1)
    checks = 0
    for u in pool:
        m = re.match(
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", u + "/", re.I
        )
        handle = m.group(1) if m else ""
        is_observed = u in observed_handles

        bio_has_domain = False
        is_valid_profile = True

        if handle and checks < limit:
            try:
                data = get_links_from_x_profile(u)

                # фильтр: несуществующие/пустые профили
                if not _is_valid_x_profile(data):
                    is_valid_profile = False
                else:
                    for b in data.get("links") or []:
                        try:
                            if (
                                urlparse(b)
                                .netloc.replace("www.", "")
                                .lower()
                                .endswith(site_domain)
                            ):
                                bio_has_domain = True
                                break
                        except Exception:
                            pass
            except Exception:
                is_valid_profile = False
            finally:
                checks += 1

        # пропуск явно невалидных (например, "New to X?")
        if not is_valid_profile:
            continue

        if handle and brand_token and brand_token.lower() in handle.lower():
            if is_observed or bio_has_domain:
                return u

        sc = score(handle, bio_has_domain=bio_has_domain, is_observed=is_observed)
        if sc > best[1]:
            best = (u, sc)

    return best[0] or current_url


# Основная функция для сбора соцсетей и docs по проекту
def collect_main_data(url, main_template, storage_path=None, max_internal_links=10):
    TRUST_HOME_TWITTER = True

    main_data = copy.deepcopy(main_template)
    found_socials = {}

    # главная страница
    html = fetch_url_html(url)
    socials = extract_social_links(html, url, is_main_page=True)
    found_socials.update({k: v for k, v in socials.items() if v})

    site_domain = urlparse(url).netloc.replace("www.", "").lower()
    brand_token = site_domain.split(".")[0]

    # twitter: подтверждение + политика "доверять домашнему"
    twitter_final = ""
    enriched_from_agg = {}

    if found_socials.get("twitterURL"):
        ok, extra = _verify_twitter_and_enrich(found_socials["twitterURL"], site_domain)
        if ok:
            twitter_final = found_socials["twitterURL"]
            enriched_from_agg = extra
            logger.info("Подтвержден по био/агрегатору: %s", twitter_final)
        else:
            logger.info(
                "Не удалось подтвердить домашний твиттер: %s",
                found_socials["twitterURL"],
            )
            if TRUST_HOME_TWITTER:
                twitter_final = found_socials["twitterURL"]
                logger.info(
                    "TRUST_HOME_TWITTER: фиксирую домашний твиттер несмотря на верификацию"
                )

    # если нет подтвержденного твиттера - поиск альтернатив (сайт/внутренние/доки/агрегаторы)
    if not twitter_final:
        candidates = set()

        # со всех видимых ссылок сайта
        candidates |= set(_extract_twitter_profiles(html, url))

        # внутренних страниц (до 5)
        for link in get_internal_links(
            html, url, max_links=min(max_internal_links, 10)
        )[:5]:
            try:
                page_html = fetch_url_html(link)
                candidates |= set(_extract_twitter_profiles(page_html, link))
            except Exception:
                pass

        # docs
        docs_html = ""
        docs_url_local = found_socials.get("documentURL")
        if docs_url_local:
            try:
                docs_html = fetch_url_html(docs_url_local)
                candidates |= set(_extract_twitter_profiles(docs_html, docs_url_local))
            except Exception:
                docs_html = ""

        # link-агрегаторы со страницы
        for coll_url in _extract_link_collection_urls(html, url):
            try:
                c_html = fetch_url_html(coll_url)
                candidates |= set(_extract_twitter_profiles(c_html, coll_url))
            except Exception:
                pass
        # и из docs (если были)
        if docs_url_local and docs_html:
            for coll_url in _extract_link_collection_urls(docs_html, docs_url_local):
                try:
                    c_html = fetch_url_html(coll_url)
                    candidates |= set(_extract_twitter_profiles(c_html, coll_url))
                except Exception:
                    pass

        # валидация кандидатов
        for u in candidates:
            ok, extra = _verify_twitter_and_enrich(u, site_domain)
            if ok:
                twitter_final = u
                enriched_from_agg = extra
                logger.info(
                    "[twitter] Найден и подтвержден альтернативный: %s", twitter_final
                )
                break

    # фикс итоговый твиттер
    if twitter_final:
        found_socials["twitterURL"] = twitter_final
    else:
        logger.info(
            "[twitter] Итог: валидного подтверждения не найдено; оставляю исходное значение: %s",
            found_socials.get("twitterURL", ""),
        )

    # обогащение соц ссылок с агрегатора (если пришло из _verify_twitter_and_enrich)
    for k, v in (enriched_from_agg or {}).items():
        if v and not found_socials.get(k):
            found_socials[k] = v

    logger.info("Соцлинков найдено: %s", found_socials)

    # сбор внутренних соцлинков с внутренних страниц
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

    # документация: дообогащение
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

    # fallback docs
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
                html_doc = resp.text
                if resp.status_code == 200 and len(html_doc) > 2200:
                    soup_doc = BeautifulSoup(html_doc, "html.parser")
                    title = (soup_doc.title.string if soup_doc.title else "").lower()
                    doc_kw = (
                        "doc" in title
                        or "documentation" in title
                        or "sidebar" in html_doc
                        or "menu" in html_doc
                        or "docs" in docs_url
                    )
                    if doc_kw:
                        logger.info("Авто-найдена docs-ссылка: %s", docs_url)
                        found_socials["documentURL"] = docs_url
                        break
            except Exception as e:
                logger.warning("Ошибка автопоиска docs (%s): %s", docs_url, e)

    # видео-слайдер YouTube (featured)
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
        "youtube.com/@" in youtube_url
        or "/channel/" in youtube_url
        or "/c/" in youtube_url
    ):
        try:
            yt_json = fetch_url_html_playwright(youtube_url)
            logger.info("Youtube-url: %s", youtube_url)
            yt_links = json.loads(yt_json)
            fv = yt_links.get("featuredVideos") or []
            if fv:
                main_data["videoSlider"] = [
                    _pack_slide(v.get("url", ""), v.get("title", ""))
                    for v in fv
                    if v.get("url")
                ]
        except Exception as e:
            logger.warning("Ошибка парса featuredVideos через browser_fetch.js: %s", e)

    # twitter: аватар и имя + добор соцлинков из био (прямые ссылки)
    avatar_url = ""
    twitter_name = ""
    if found_socials.get("twitterURL"):
        twitter_result = get_links_from_x_profile(found_socials["twitterURL"])
        bio_links = twitter_result.get("links", [])
        avatar_url = twitter_result.get("avatar", "")
        raw_twitter_name = twitter_result.get("name", "")
        twitter_name = re.split(r"\||-", (raw_twitter_name or "").strip())[0].strip()

        def _dom(href: str) -> str:
            try:
                return urlparse(href).netloc.replace("www.", "").lower()
            except Exception:
                return ""

        for bio_url in bio_links:
            d = _dom(bio_url)
            for k in main_template["socialLinks"].keys():
                if not k.endswith("URL") or found_socials.get(k):
                    continue
                key = k.replace("URL", "").lower()
                if (
                    (key == "twitter" and d in ("x.com", "twitter.com"))
                    or (key == "telegram" and d in ("t.me", "telegram.me"))
                    or (key == "discord" and d in ("discord.gg", "discord.com"))
                    or (key == "youtube" and ("youtube.com" in d or d == "youtu.be"))
                    or (key == "medium" and d == "medium.com")
                    or (key == "github" and d == "github.com")
                    or (key == "linkedin" and d == "linkedin.com")
                    or (key == "reddit" and d == "reddit.com")
                ):
                    found_socials[k] = bio_url

    # имя проекта
    soup = BeautifulSoup(html, "html.parser")
    raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""

    title_parts = [
        clean_project_name(p) for p in re.split(r"[|\-–—·•:]+", raw_title) if p.strip()
    ]
    title_parts = [p for p in title_parts if p and not is_bad_name(p)]

    best_title = ""
    for p in title_parts:
        if brand_token and brand_token.lower() in p.lower():
            best_title = p
            break
    if not best_title and title_parts:
        best_title = max(title_parts, key=len)

    clean_twitter = clean_project_name(twitter_name)
    brand_from_domain = clean_project_name(brand_token)

    candidates = []
    if clean_twitter and not is_bad_name(clean_twitter):
        candidates.append(clean_twitter)
    if best_title and not is_bad_name(best_title):
        candidates.append(best_title)
    if brand_from_domain and not is_bad_name(brand_from_domain):
        candidates.append(brand_from_domain.capitalize())

    project_name = ""
    for c in candidates:
        if brand_token and brand_token.lower() in c.lower():
            project_name = c
            break
    if not project_name and candidates:
        project_name = candidates[0]
    if not project_name:
        project_name = get_domain_name(url).capitalize()

    # fail-safe
    if (
        not project_name
        or not isinstance(project_name, str)
        or not project_name.strip()
    ):
        project_name = get_domain_name(url).capitalize()
    main_data["name"] = project_name

    logger.info("Итоговое имя проекта: '%s'", project_name)

    # аватарка X
    logo_filename = f"{project_name.lower().replace(' ', '')}.jpg"

    if not found_socials.get("twitterURL"):
        logger.warning("twitterURL отсутствует — пропускаю скачивание аватара")
    elif not avatar_url:
        logger.warning(
            "avatar пуст - парсер не вернул URL для %s",
            found_socials.get("twitterURL"),
        )
    elif not storage_path:
        logger.warning("storage_path пуст - некуда сохранять аватар")
    else:
        avatar_path = os.path.join(storage_path, logo_filename)
        try:
            headers_img = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*;q=0.8,*/*;q=0.5",
                "Referer": found_socials.get("twitterURL") or "https://x.com/",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
            }

            def _get_image_with_retry(url_img, headers, tries=3, timeout=25):
                last = None
                retryable = {403, 429, 500, 502, 503, 504}
                for i in range(tries):
                    try:
                        r = requests.get(
                            url_img,
                            timeout=timeout,
                            headers=headers,
                            allow_redirects=True,
                        )
                        if r.status_code == 200 and r.content:
                            return r
                        last = r
                        if r.status_code in retryable:
                            import time

                            time.sleep(1.0 + 0.5 * i)
                            continue
                        break
                    except Exception as e:
                        last = None
                        logger.warning("Ошибка запроса (try %s): %s", i + 1, e)
                        import time

                        time.sleep(0.8)
                return last

            avatar_url_raw = force_https(avatar_url)
            logger.info("Avatar URL: %s", avatar_url_raw)

            resp_img = _get_image_with_retry(
                avatar_url_raw, headers_img, tries=3, timeout=25
            )

            if not resp_img:
                logger.warning(
                    "Не скачан: нет ответа от сервера, url=%s", avatar_url_raw
                )
            else:
                ct = (resp_img.headers.get("Content-Type") or "").lower()
                if (
                    resp_img.status_code == 200
                    and resp_img.content
                    and (
                        "image/" in ct
                        or avatar_url_raw.startswith("https://pbs.twimg.com/")
                    )
                ):
                    with open(avatar_path, "wb") as imgf:
                        imgf.write(resp_img.content)
                    main_data["svgLogo"] = logo_filename
                    logger.info("Сохранен: %s", avatar_path)
                else:
                    logger.warning(
                        "Не скачан: code=%s, ct=%s, url=%s",
                        getattr(resp_img, "status_code", "no-response"),
                        ct,
                        avatar_url_raw,
                    )

        except Exception as e:
            logger.warning("Ошибка скачивания аватара: %s", e)

    if "svgLogo" not in main_data:
        main_data["svgLogo"] = ""

    # финальная сборка socialLinks
    social_keys = list(main_template["socialLinks"].keys())
    final_socials = {k: found_socials.get(k, "") for k in social_keys}
    final_socials = normalize_socials(final_socials)
    main_data["socialLinks"] = final_socials

    return main_data
