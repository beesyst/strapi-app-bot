from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Dict, List, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger
from core.parser import nitter as nitter_mod
from core.parser.link_aggregator import (
    extract_socials_from_aggregator,
    is_link_aggregator,
)
from core.parser.link_aggregator import (
    find_aggregators_in_links as _find_aggs_in_links,
)
from core.parser.link_aggregator import (
    verify_aggregator_belongs as _verify_agg_belongs,
)
from core.parser.web import fetch_url_html
from core.paths import PROJECT_ROOT

logger = get_logger("twitter")

ROOT_DIR = PROJECT_ROOT

# Флаг и настройки Playwright-догрузки X-профиля
TW_PLAYWRIGHT_ENABLED = True


# Вспомогательная функция: привести строку к нижнему регистру и оставить только [a-z0-9]
def _norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Вспомогательная функция: вытащить handle из URL вида https://x.com/handle
def _handle_from_url(u: str) -> str:
    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (u or "") + "/", re.I
    )
    return m.group(1) if m else ""


# Вспомогательная функция: строгая проверка совпадения brand_token и handle
def _strict_site_match_handle(brand_token: str, handle: str) -> bool:
    return (brand_token or "").lower() == (handle or "").lower()


# Вспомогательная функция: нормализовать протокол в https и обрезать мусор
def force_https(url: str) -> str:
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Вспомогательная функция: вернуть host без www
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Вспомогательная функция: канонизировать X-профиль (twitter.com -> x.com, без query/fragment)
def normalize_twitter_url(u: str) -> str:
    if not u:
        return u
    u = force_https(u.strip())
    u = re.sub(r"^https://twitter\.com", "https://x.com", u, flags=re.I)
    u = re.sub(r"[?#].*$", "", u)
    u = re.sub(
        r"/(photo|media|with_replies|likes|lists|following|followers)/?$",
        "",
        u,
        flags=re.I,
    )
    m = re.match(r"^https://x\.com/([A-Za-z0-9_]{1,15})/?$", u, re.I)
    if m:
        handle = m.group(1).lower()
        return f"https://x.com/{handle}"
    return u.rstrip("/")


# Вспомогательная функция: нормализовать URL аватарки (включая /pic/ от Nitter)
def normalize_twitter_avatar(url: str) -> str:
    u = force_https(url or "")
    if not u:
        return ""

    # Nitter-перенаправления и проценты
    if u.startswith("/pic/") or "%2F" in u or "%3A" in u:
        u = _decode_nitter_pic_url(u)

    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u

    # убрать query/fragment
    u = re.sub(r"(?:\?[^#]*)?(?:#.*)?$", "", u)

    # привести к сырому виду без _200x200,_400x400,_normal,_bigger,_mini и т.п.
    if "pbs.twimg.com/profile_images/" in u:
        u = re.sub(
            r"(/profile_images/[^/]+/[^/.]+?)"
            r"(?:_[0-9]+x[0-9]+|_x[0-9]+|_normal|_bigger|_mini)"
            r"(\.[a-zA-Z0-9]+)$",
            r"\1\2",
            u,
        )

    return u


# Вспомогательная функция: декодировать /pic/... от Nitter в нормальный https-URL
def _decode_nitter_pic_url(src: str) -> str:
    if not src:
        return ""
    s = src.strip()
    if s.startswith("/pic/"):
        s = s[len("/pic/") :]
    s = unquote(s)
    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("http://"):
        s = "https://" + s[7:]
    elif s.startswith("https://"):
        pass
    else:
        s = "https://" + s.lstrip("/")
    return s


# Кэш уже разобранных X-профилей
_PARSED_X_PROFILE_CACHE: Dict[str, Dict] = {}

# Набор URL-ов, для которых уже логировали Playwright GET+parse
_PLAYWRIGHT_LOGGED: set[str] = set()


# Вспомогательная функция: распарсить HTML X-профиля (после Playwright) - ссылки, имя, аватар
def _parse_x_profile_html(html: str) -> Dict[str, object]:
    soup = BeautifulSoup(html or "", "html.parser")

    # display name
    name = ""
    name_el = soup.select_one('[data-testid="UserName"] span') or soup.select_one(
        'h2[role="heading"] > div > span'
    )
    if name_el:
        name = (name_el.get_text(strip=True) or "").strip()

    # фолбэк по <title>
    if not name:
        title_tag = soup.title.string if soup.title and soup.title.string else ""
        t = (title_tag or "").strip()
        m = re.match(r"^(.+?)\s*\(", t) or re.match(r"^(.+?)\s*\/\s", t)
        name = (m.group(1) if m else t).strip()

    links: set[str] = set()
    handles: set[str] = set()

    # bio
    bio = soup.select_one('[data-testid="UserDescription"]')
    if bio:
        # ссылки из <a> внутри BIO (работаем по DOM, не по regex по всему HTML)
        for a in bio.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href:
                continue

            # @handles вида href="/Name" → складываем в handles
            m_handle = re.match(r"^/([A-Za-z0-9_]{1,15})/?$", href)
            if m_handle:
                handles.add("@" + m_handle.group(1))
                continue

            # нас интересуют только http(s)-ссылки
            if not href.startswith("http"):
                continue

            u = force_https(href)
            h = _host(u)

            # t.co в bio: разворачиваем по видимому тексту (как уже делаем в header)
            if h == "t.co":
                visible = a.get_text(" ", strip=True) or ""
                # пример: "https://berapaw.com" или "https:// berapaw.com"
                for naked in re.findall(
                    r"([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]+)?)",
                    visible,
                ):
                    naked = naked.strip().rstrip(".,;:!?)(")
                    if not naked:
                        continue

                    # если протокола нет - добавляем https://
                    url_from_text = (
                        naked if naked.startswith("http") else "https://" + naked
                    )
                    url_from_text = force_https(url_from_text)
                    h_text = _host(url_from_text)

                    # режем служебные домены
                    if (
                        h_text
                        and h_text not in ("x.com", "twitter.com", "t.co")
                        and not h_text.endswith(".x.com")
                        and not h_text.endswith(".twimg.com")
                    ):
                        links.add(url_from_text)
                continue

            # обычные внешние ссылки из BIO, кроме служебных
            if (
                h in ("x.com", "twitter.com", "t.co")
                or h.endswith(".x.com")
                or h.endswith(".twimg.com")
            ):
                continue

            links.add(u)

        # запасной вариант - голые домены в тексте BIO (без <a>)
        text = bio.get_text(" ", strip=True) or ""
        for naked in re.findall(r"([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]+)?)", text):
            naked = naked.strip().rstrip(".,;:!?)(")
            if not naked:
                continue

            host_name = _host("https://" + naked)
            if (
                host_name in ("t.co", "x.com", "twitter.com")
                or host_name.endswith(".x.com")
                or host_name.endswith(".twimg.com")
            ):
                continue

            # если этот домен уже содержится внутри какой-то найденной ссылки - скип
            if any(naked in l for l in links):
                continue

            links.add(force_https("https://" + naked))

    # header (UserProfileHeader_Items): сайт/discord и т.п.
    header_items = soup.select_one('[data-testid="UserProfileHeader_Items"]')
    if header_items:
        for a in header_items.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue

            # t.co -> разворачиваем по видимому тексту (discord.gg/..., сайт и т.п.)
            if href.startswith("https://t.co/"):
                span = a.select_one("span")
                text = (
                    span.get_text(strip=True) if span else a.get_text(" ", strip=True)
                ) or ""

                # простой паттерн домена/URL в тексте
                if re.match(r"^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]+)?$", text):
                    url_from_text = force_https("https://" + text)
                    h_text = _host(url_from_text)
                    if (
                        h_text
                        and h_text not in ("x.com", "twitter.com", "t.co")
                        and not h_text.endswith(".x.com")
                        and not h_text.endswith(".twimg.com")
                    ):
                        links.add(url_from_text)
                continue

            # прямые внешние http-ссылки, кроме twitter/x/t.co
            if href.startswith("http"):
                u_norm = force_https(href)
                h = _host(u_norm)
                if (
                    h in ("x.com", "twitter.com", "t.co")
                    or h.endswith(".x.com")
                    or h.endswith(".twimg.com")
                ):
                    continue
                links.add(u_norm)

    # аватар
    avatar = ""
    avatar_container = soup.select_one('[data-testid^="UserAvatar-Container"]')
    if avatar_container:
        img_el = avatar_container.find("img", src=True)
        if img_el:
            avatar = img_el.get("src", "")

        if not avatar:
            bg_div = avatar_container.select_one('div[style*="background-image"]')
            if bg_div:
                style = bg_div.get("style") or ""
                m = re.search(
                    r'url\(["\']?(https?:\/\/[^"\')]+)["\']?\)',
                    style,
                    re.I,
                )
                if m:
                    avatar = m.group(1)

    if not avatar:
        img = soup.find("img", src=re.compile(r"pbs\.twimg\.com/profile_images/"))
        if img and img.get("src"):
            avatar = img["src"]

    if not avatar:
        for div in soup.select('div[style*="background-image"]'):
            style = div.get("style") or ""
            m = re.search(
                r'url\(["\']?(https?:\/\/[^"\')]+profile_images[^"\')]+)["\']?\)',
                style,
                re.I,
            )
            if m:
                avatar = m.group(1)
                break

    if not avatar:
        meta = soup.find("meta", attrs={"property": "og:image"}) or soup.find(
            "meta", attrs={"name": "og:image"}
        )
        if meta and meta.get("content"):
            og = meta["content"]
            if "pbs.twimg.com/profile_images/" in og:
                avatar = og

    if not handles:
        chunks: list[str] = []
        if bio:
            chunks.append(bio.get_text(" ", strip=True) or "")
        if "header_items" in locals() and header_items:
            chunks.append(header_items.get_text(" ", strip=True) or "")
        text_all = " ".join(chunks) if chunks else ""
        for m in re.findall(r"@([A-Za-z0-9_]{1,15})", text_all):
            handles.add("@" + m)

    if avatar:
        avatar = normalize_twitter_avatar((avatar or "").replace("&amp;", "&"))

        pass

    return {
        "links": list(links),
        "avatar": avatar,
        "name": name,
        "handles": list(handles),
    }


# Вспомогательная функция: вытащить первый JSON-объект из stdout/stderr Node-скрипта
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


# Основная функция: получить ссылки, имя и аватар из X-профиля (Nitter + Playwright)
def get_links_from_x_profile(
    profile_url: str, need_avatar: bool = True
) -> Dict[str, object]:
    script_path = os.path.join(ROOT_DIR, "core", "parser", "browser_fetch.js")

    orig_url = (profile_url or "").strip()
    if not orig_url:
        return {"links": [], "avatar": "", "name": ""}

    safe_url = normalize_twitter_url(orig_url)

    # кэш по каноническому URL
    cached = _PARSED_X_PROFILE_CACHE.get(safe_url)
    if cached:
        has_avatar = bool((cached.get("avatar") or "").strip())
        if (not need_avatar) or has_avatar:
            logger.debug(
                "X-профиль из кэша: %s (need_avatar=%s, avatar=%s)",
                safe_url,
                need_avatar,
                "yes" if has_avatar else "no",
            )
            return cached
        # если нужен аватар, а в кэше его нет - будем пытаться дозаполнить
        parsed_links = list(cached.get("links") or [])
        parsed_avatar = cached.get("avatar") or ""
        parsed_name = cached.get("name") or ""
    else:
        parsed_links = []
        parsed_avatar = ""
        parsed_name = ""

    # попытка обогатить профиль через Nitter (логика и конфиг в core/parser/nitter.py)
    try:
        nitter_data = nitter_mod.parse_profile(safe_url) or {}
    except Exception as e:
        logger.warning("Nitter parse_profile error for %s: %s", safe_url, e)
        nitter_data = {}

    if isinstance(nitter_data, dict) and (
        nitter_data.get("links")
        or nitter_data.get("avatar")
        or nitter_data.get("avatar_raw")
        or nitter_data.get("name")
    ):
        for l in nitter_data.get("links") or []:
            if isinstance(l, str) and l:
                l_norm = force_https(l)
                if l_norm not in parsed_links:
                    parsed_links.append(l_norm)

        if not parsed_avatar:
            parsed_avatar = (
                nitter_data.get("avatar")
                or nitter_data.get("avatar_raw")
                or parsed_avatar
            )

        if not parsed_name:
            parsed_name = nitter_data.get("name") or parsed_name

    has_any_profile = bool(parsed_links or parsed_name or parsed_avatar)

    # при необходимости - фолбек через Playwright на x.com (ОДИН заход)
    need_playwright = TW_PLAYWRIGHT_ENABLED and (
        (need_avatar and not parsed_avatar) or (not has_any_profile)
    )

    def _run_once(u: str):
        try:
            return subprocess.run(
                [
                    "node",
                    script_path,
                    u,
                    "--html",
                    "--twitterProfile",
                    "true",
                    "--wait",
                    "domcontentloaded",
                    "--timeout",
                    "45000",
                    "--scrollPages",
                    "2",
                    "--waitSocialHosts",
                    "t.co,discord.gg,github.com,linktr.ee,t.me,youtube.com,medium.com,reddit.com",
                ],
                cwd=os.path.dirname(script_path),
                capture_output=True,
                text=True,
                timeout=90,
            )
        except Exception as e:
            logger.warning("Ошибка запуска browser_fetch.js для %s: %s", u, e)
            return None

    if need_playwright:
        # всегда только один заход на сам профиль
        u = safe_url
        result = _run_once(u)
        if result:
            stdout = result.stdout or ""
            stderr = result.stderr or ""

            # основная попытка: stdout → JSON { ok, html, ... }
            data: dict = {}
            try:
                if stdout.strip().startswith("{"):
                    data = json.loads(stdout)
                else:
                    data = _extract_first_json_object(stdout) or {}
            except Exception:
                data = _extract_first_json_object(stdout) or {}

            html_from_browser = ""
            twitter_profile = None

            if isinstance(data, dict):
                if isinstance(data.get("html"), str):
                    html_from_browser = data.get("html") or ""
                if isinstance(data.get("twitter_profile"), dict):
                    twitter_profile = data["twitter_profile"]

            # приоритет: готовый twitter_profile из JS
            browser_parsed: dict = {}

            # пробуем взять то, что дал JS (twitter_profile)
            if twitter_profile:
                try:
                    raw_links = twitter_profile.get("links") or []

                    # фильтруем мусор: служебные домены X/Twitter/t.co и чужие профили
                    current_handle = _handle_from_url(safe_url)
                    filtered_links: list[str] = []

                    for l in raw_links:
                        if not isinstance(l, str) or not l.strip():
                            continue
                        u = force_https(l)
                        h = _host(u)
                        if not h:
                            continue

                        # служебные домены X/Twitter/t.co полностью выкидываем из links
                        if (
                            h in ("x.com", "twitter.com", "t.co")
                            or h.endswith(".x.com")
                            or h.endswith(".twimg.com")
                        ):
                            continue

                        filtered_links.append(u)

                    # убираем дубликаты, сохраняя порядок
                    seen = set()
                    clean_links: list[str] = []
                    for u in filtered_links:
                        if u not in seen:
                            clean_links.append(u)
                            seen.add(u)

                    browser_parsed = {
                        "links": clean_links,
                        "avatar": twitter_profile.get("avatar") or "",
                        "name": twitter_profile.get("name") or "",
                        "handles": twitter_profile.get("handles") or [],
                    }
                except Exception as e:
                    logger.warning(
                        "Ошибка обработки twitter_profile для %s: %s",
                        safe_url,
                        e,
                    )
                    browser_parsed = {}

            # всегда пытаемся дообогатить HTML-парсером (bio, агрегаторы и т.п.)
            if html_from_browser.strip():
                try:
                    html_parsed = _parse_x_profile_html(html_from_browser)
                except Exception as e:
                    logger.warning(
                        "Ошибка парсинга HTML X-профиля через Playwright для %s: %s",
                        safe_url,
                        e,
                    )
                    html_parsed = {}

                if isinstance(html_parsed, dict):
                    # если twitter_profile ничего не дал - берем HTML-результат как есть
                    if (
                        not browser_parsed.get("links")
                        and not browser_parsed.get("avatar")
                        and not browser_parsed.get("name")
                    ):
                        browser_parsed = html_parsed
                    else:
                        # мержим: ссылки + аватар + имя в общий parsed_links
                        for l in html_parsed.get("links") or []:
                            if isinstance(l, str) and l:
                                l_norm = force_https(l)
                                if l_norm not in parsed_links:
                                    parsed_links.append(l_norm)
                        if not parsed_avatar and html_parsed.get("avatar"):
                            parsed_avatar = html_parsed["avatar"]
                        if not parsed_name and html_parsed.get("name"):
                            parsed_name = html_parsed["name"]

                        if not browser_parsed.get("links") and html_parsed.get("links"):
                            browser_parsed["links"] = list(
                                html_parsed.get("links") or []
                            )

                        if (
                            not browser_parsed.get("handles")
                            and isinstance(html_parsed.get("handles"), list)
                            and html_parsed.get("handles")
                        ):
                            browser_parsed["handles"] = list(html_parsed["handles"])

            if not browser_parsed:
                browser_parsed = {}

            # если удалось что-то вытащить (links/avatar/name) - мержим
            if isinstance(browser_parsed, dict) and (
                browser_parsed.get("links")
                or browser_parsed.get("avatar")
                or browser_parsed.get("name")
            ):
                # оставляем только внешние BIO/шапка-ссылки, без служебных доменов X
                browser_links: list[str] = []
                for l in browser_parsed.get("links") or []:
                    if not isinstance(l, str) or not l.strip():
                        continue
                    u_norm = force_https(l)
                    h = _host(u_norm)
                    if (
                        not h
                        or h in ("x.com", "twitter.com", "t.co")
                        or h.endswith(".x.com")
                        or h.endswith(".twimg.com")
                    ):
                        # выкидываем всё, что относится к самому X/Twitter
                        continue
                    browser_links.append(u_norm)
                    if u_norm not in parsed_links:
                        parsed_links.append(u_norm)

                if not parsed_avatar:
                    parsed_avatar = browser_parsed.get("avatar") or parsed_avatar

                if not parsed_name:
                    parsed_name = browser_parsed.get("name") or parsed_name

                cleaned = {
                    "links": parsed_links,
                    "avatar": normalize_twitter_avatar(parsed_avatar or ""),
                    "name": parsed_name or "",
                }
                _PARSED_X_PROFILE_CACHE[safe_url] = cleaned

                try:
                    if safe_url not in _PLAYWRIGHT_LOGGED:
                        # считаем кол-во ссылок по финальному cleaned, а не только по raw browser_links
                        links_for_log = cleaned.get("links") or []

                        logger.info(
                            "Playwright GET+parse: %s → avatar=%s, links=%d",
                            safe_url,
                            "yes" if cleaned.get("avatar") else "no",
                            len(links_for_log),
                        )

                        # BIO X (Playwright): берем ссылки из финальных cleaned["links"],
                        browser_handles = browser_parsed.get("handles") or []

                        bio_log_links: list[str] = []

                        # сначала используем browser_links, если они есть
                        if browser_links:
                            bio_log_links = list(dict.fromkeys(browser_links))
                        else:
                            # если browser_links пустой, строим список из cleaned["links"],
                            for l in links_for_log:
                                if not isinstance(l, str) or not l.strip():
                                    continue
                                u_norm = force_https(l)
                                h = _host(u_norm)
                                if (
                                    not h
                                    or h in ("x.com", "twitter.com", "t.co")
                                    or h.endswith(".x.com")
                                    or h.endswith(".twimg.com")
                                ):
                                    continue
                                bio_log_links.append(u_norm)
                            # убираем дубли, сохраняя порядок
                            bio_log_links = list(dict.fromkeys(bio_log_links))

                        if bio_log_links or browser_handles:
                            logger.info(
                                "BIO X (Playwright): links=%s, handles=%s",
                                bio_log_links,
                                list(dict.fromkeys(browser_handles)),
                            )

                        _PLAYWRIGHT_LOGGED.add(safe_url)
                    else:
                        logger.debug(
                            "Playwright GET+parse (cached url): %s → avatar=%s, links=%d",
                            safe_url,
                            "yes" if cleaned.get("avatar") else "no",
                            len((cleaned.get("links") or [])),
                        )
                except Exception:
                    logger.info("Playwright GET+parse: %s", safe_url)

                return cleaned

            # regex-фолбэк: ищем pbs.twimg.com в html/логе
            blob = html_from_browser or (stdout + "\n" + stderr)
            m_img = re.search(
                r"https://pbs\.twimg\.com/profile_images/[^\s\"']+\.(?:jpg|jpeg|png|webp)",
                blob,
                re.I,
            )
            if m_img and not parsed_avatar:
                parsed_avatar = m_img.group(0)
                cleaned = {
                    "links": parsed_links,
                    "avatar": normalize_twitter_avatar(parsed_avatar or ""),
                    "name": parsed_name or "",
                }
                _PARSED_X_PROFILE_CACHE[safe_url] = cleaned
                if safe_url not in _PLAYWRIGHT_LOGGED:
                    logger.info(
                        "Playwright GET+parse (regex-fallback): %s → avatar=%s, links=%d",
                        safe_url,
                        "yes" if cleaned.get("avatar") else "no",
                        len(cleaned.get("links") or []),
                    )
                    _PLAYWRIGHT_LOGGED.add(safe_url)
                else:
                    logger.debug(
                        "Playwright GET+parse (regex-fallback, cached url): %s → avatar=%s, links=%d",
                        safe_url,
                        "yes" if cleaned.get("avatar") else "no",
                        len(cleaned.get("links") or []),
                    )
                return cleaned

    # финальная нормализация и возврат (даже если ничего не нашли)
    cleaned_final = {
        "links": parsed_links,
        "avatar": normalize_twitter_avatar(parsed_avatar or ""),
        "name": parsed_name or "",
    }
    _PARSED_X_PROFILE_CACHE[safe_url] = cleaned_final
    return cleaned_final


# Функция: найти все X-профили в HTML (по ссылкам <a> и "голым" упоминаниям)
def extract_twitter_profiles(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    profiles = set()

    # ссылки из <a>
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

    # "голые" упоминания
    text = html or ""
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

    out = list(profiles)
    if out:
        logger.debug("Список X-профилей на %s: %s", base_url, out)
    return out


# Функция: найти ссылки на линк-агрегаторы (linktree и т.п.) на странице
def extract_link_collection_urls(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if is_link_aggregator(href):
            urls.append(force_https(href))
    return urls


# Вспомогательная функция: профиль X считается валидным, если он не "пустышка"
def _is_valid_x_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    name = (parsed.get("name") or "").strip().lower()
    avatar = (parsed.get("avatar") or "").strip()
    links = parsed.get("links") or []
    if (not avatar) and (not name or "new to x" in name) and (not links):
        return False
    return True


# Функция: проверить twitter_url по bio/агрегатору/сайту и вернуть (ok, enriched_socials, agg_url)
def verify_twitter_and_enrich(
    twitter_url: str, site_domain: str
) -> Tuple[bool, dict, str]:
    if _VERIFIED_TW_URL and normalize_twitter_url(twitter_url) == normalize_twitter_url(
        _VERIFIED_TW_URL
    ):
        return True, dict(_VERIFIED_ENRICHED), _VERIFIED_AGG_URL

    data = get_links_from_x_profile(twitter_url, need_avatar=False)
    if not _is_valid_x_profile(data):
        return False, {}, ""

    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
        (twitter_url or "") + "/",
        re.I,
    )
    handle = m.group(1) if m else ""
    bio_links = [force_https(b) for b in (data.get("links") or [])]

    # агрегаторы из bio
    aggs = _find_aggs_in_links(bio_links)
    for agg_url in aggs:
        ok, _ = _verify_agg_belongs(agg_url, site_domain, handle)
        if ok:
            socials_clean = extract_socials_from_aggregator(agg_url) or {}
            if site_domain:
                socials_clean["websiteURL"] = f"https://www.{site_domain}/"

            agg_url_log = force_https(agg_url)

            logger.info(
                "Агрегатор подтвержден (%s) и дал соц-ссылки: %s",
                agg_url_log,
                {k: v for k, v in socials_clean.items() if v},
            )

            return True, socials_clean, force_https(agg_url)

    # прямой сайт в bio - ок
    for b in bio_links:
        try:
            if _host(b).endswith(site_domain):
                return True, {"websiteURL": f"https://www.{site_domain}/"}, ""
        except Exception:
            pass

    return False, {}, (force_https(aggs[0]) if aggs else "")


# Вспомогательная функция: проверить, что агрегатор содержит и сайт, и handle проекта
def _agg_has_site_and_handle(agg_url: str, site_domain: str, handle: str) -> str:
    try:
        html = fetch_url_html(agg_url, prefer="http", timeout=25)
    except Exception:
        return ""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""

    site_hit = ""
    handle_ok = False
    handle_lc = (handle or "").lower()

    for a in soup.find_all("a", href=True):
        href = force_https(urljoin(agg_url, a["href"]))
        host = _host(href)

        if site_domain and host.endswith(site_domain):
            site_hit = href

        if re.search(
            r"(?:^|/)(?:x\.com|twitter\.com)/" + re.escape(handle_lc) + r"(?:/|$)",
            href,
            re.I,
        ):
            handle_ok = True

    return site_hit if (site_hit and handle_ok) else ""


# Функция: загрузить страницу и извлечь все X-профили
def fetch_and_extract_twitter_profiles(from_url: str) -> List[str]:
    try:
        html = fetch_url_html(from_url, prefer="auto", timeout=30)
        return extract_twitter_profiles(html, from_url)
    except Exception:
        return []


# Функция: сгенерировать возможные X-хэндлы по бренду (<=15 символов)
def guess_twitter_handles(brand_token: str) -> List[str]:
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
    clean = []
    for v in variants:
        v = re.sub(r"[^a-z0-9_]", "", v)
        if 1 <= len(v) <= 15:
            clean.append(v)
    seen, out = set(), []
    for h in clean:
        if h not in seen:
            out.append(h)
            seen.add(h)
    return out


# Функция: выбрать лучший X-профиль по скорингу (bio, домен, observed-список)
def pick_best_twitter(
    current_url: str | None,
    candidates: List[str],
    brand_token: str,
    site_domain: str,
    max_profile_checks: int | None = None,
    observed_handles: List[str] | None = None,
) -> str | None:
    observed = set(observed_handles or [])

    def score(handle: str, bio_has_domain=False, is_observed=False) -> int:
        s = 0
        if brand_token and brand_token in (handle or "").lower():
            s += 100
        if bio_has_domain:
            s += 80
        if is_observed:
            s += 40
        if brand_token and (handle or "").lower().startswith(brand_token.lower()):
            s += 20
        return s

    seen = set()
    pool = []
    for u in candidates or []:
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
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (u or "") + "/", re.I
        )
        handle = m.group(1) if m else ""
        is_observed = u in observed

        bio_has_domain = False
        is_valid_profile = True

        if handle and checks < limit:
            try:
                data = get_links_from_x_profile(u, need_avatar=False)
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

        if not is_valid_profile:
            continue

        if handle and brand_token and brand_token.lower() in handle.lower():
            if is_observed or bio_has_domain:
                return u

        sc = score(handle, bio_has_domain=bio_has_domain, is_observed=is_observed)
        if sc > best[1]:
            best = (u, sc)

    return best[0] or current_url


_VERIFIED_TW_URL: str = ""
_VERIFIED_AGG_URL: str = ""
_VERIFIED_ENRICHED: dict = {}
_VERIFIED_DOMAIN: str = ""


# Функция: верифицировать "домашний" X-профиль (из found_socials)
def decide_home_twitter(
    home_twitter_url: str, site_domain: str, trust_home: bool = True
):
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL
    if not home_twitter_url:
        return "", {}, False, ""

    ok, extra, agg_url = verify_twitter_and_enrich(home_twitter_url, site_domain)
    norm = normalize_twitter_url(home_twitter_url)

    if ok:
        logger.info("X подтвержден: %s (home_twitter)", norm)
        _VERIFIED_TW_URL = norm
        _VERIFIED_ENRICHED = dict(extra or {})
        _VERIFIED_AGG_URL = agg_url or ""
        return norm, (extra or {}), True, (agg_url or "")

    return "", {}, False, ""


# Функция: найти и зафиксировать один верифицированный X в рамках парсинга проекта
def select_verified_twitter(
    found_socials: dict,
    socials: dict,
    site_domain: str,
    brand_token: str,
    html: str,
    url: str,
    trust_home: bool = False,
) -> tuple[str, dict, str, str]:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL, _VERIFIED_DOMAIN

    if (
        _VERIFIED_TW_URL
        and (_VERIFIED_DOMAIN or "").lower() == (site_domain or "").lower()
    ):
        return (
            _VERIFIED_TW_URL,
            dict(_VERIFIED_ENRICHED),
            _VERIFIED_AGG_URL,
            "",
        )

    twitter_final = ""
    enriched_from_agg = {}
    aggregator_url = ""

    # домашний twitterURL из found_socials
    if found_socials.get("twitterURL"):
        t_final, t_extra, _verified, agg_url = decide_home_twitter(
            home_twitter_url=found_socials["twitterURL"],
            site_domain=site_domain,
            trust_home=trust_home,
        )
        if t_final:
            _VERIFIED_TW_URL = twitter_final = normalize_twitter_url(t_final)
            _VERIFIED_ENRICHED = dict(t_extra or {})
            _VERIFIED_AGG_URL = aggregator_url = agg_url or ""
            _VERIFIED_DOMAIN = (site_domain or "").lower()
            # ава из профиля
            avatar_url = ""
            try:
                prof = get_links_from_x_profile(twitter_final, need_avatar=True)
                avatar_url = (prof or {}).get("avatar", "") or ""
            except Exception:
                avatar_url = ""
            return twitter_final, dict(t_extra or {}), aggregator_url, avatar_url

    # сбор кандидатов с сайта/доков/агрегаторов на страницах
    browser_twitter_ordered = []
    if isinstance(socials, dict) and isinstance(socials.get("twitterAll"), list):
        browser_twitter_ordered = [
            u for u in socials["twitterAll"] if isinstance(u, str) and u
        ]

    candidates_ordered = list(browser_twitter_ordered)

    def _extract_twitter_profiles_from(html_text: str, base: str):
        try:
            return extract_twitter_profiles(html_text, base)
        except Exception:
            return []

    candidates_ordered.extend(_extract_twitter_profiles_from(html, url))

    docs_html = ""
    docs_url_local = found_socials.get("documentURL")
    if docs_url_local:
        try:
            docs_html = fetch_url_html(docs_url_local)
            candidates_ordered.extend(
                _extract_twitter_profiles_from(docs_html, docs_url_local)
            )
        except Exception:
            docs_html = ""

    for coll_url in extract_link_collection_urls(html, url):
        try:
            c_html = fetch_url_html(coll_url)
            candidates_ordered.extend(_extract_twitter_profiles_from(c_html, coll_url))
        except Exception:
            pass

    if docs_url_local and docs_html:
        for coll_url in extract_link_collection_urls(docs_html, docs_url_local):
            try:
                c_html = fetch_url_html(coll_url)
                candidates_ordered.extend(
                    _extract_twitter_profiles_from(c_html, coll_url)
                )
            except Exception:
                pass

    # дедуп/нормализация
    seen = set()
    deduped = []
    for u in candidates_ordered:
        if not isinstance(u, str) or not u:
            continue
        u_norm = normalize_twitter_url(u)
        if u_norm not in seen:
            deduped.append(u_norm)
            seen.add(u_norm)

    def _handle_from_url_local(u: str) -> str:
        m = re.match(
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (u or "") + "/", re.I
        )
        return (m.group(1) if m else "").lower()

    def _strict_site_match_handle_local(bt: str, h: str) -> bool:
        bt = (bt or "").lower().replace("-", "").replace("_", "")
        h2 = (h or "").lower().replace("-", "").replace("_", "")
        return bool(bt and h2 and (bt == h2 or h2.startswith(bt) or bt in h2))

    bt = (brand_token or "").lower()
    dom_set = {normalize_twitter_url(u) for u in browser_twitter_ordered}

    # хедер/футер/меню
    for u in deduped:
        if u in dom_set:
            h = _handle_from_url_local(u)
            if _strict_site_match_handle_local(bt, h):
                logger.debug("X подтвержден по ссылке с сайта (handle≈brand): %s", u)
            else:
                logger.debug(
                    "X подтвержден по ссылке с сайта (handle≠brand, но доверяем сайту): %s",
                    u,
                )
            logger.info("X подтвержден: %s", u)

            _VERIFIED_TW_URL = twitter_final = u
            _VERIFIED_ENRICHED = {}
            _VERIFIED_AGG_URL = ""
            _VERIFIED_DOMAIN = (site_domain or "").lower()

            avatar_url = ""
            try:
                prof = get_links_from_x_profile(u, need_avatar=True)
                avatar_url = (prof or {}).get("avatar", "") or ""
            except Exception:
                avatar_url = ""
            return twitter_final, {}, "", avatar_url

    # обычный порядок проверок (bio/агрегатор/скоринг)
    def _handle_contains_brand(u: str) -> bool:
        h = _handle_from_url_local(u)
        return bool(bt and h and bt in h)

    first_pass = [u for u in deduped if u in dom_set and _handle_contains_brand(u)]
    second_pass = [u for u in deduped if u in dom_set and not _handle_contains_brand(u)]
    third_pass = [u for u in deduped if u not in dom_set and _handle_contains_brand(u)]
    fourth_pass = [
        u for u in deduped if u not in dom_set and not _handle_contains_brand(u)
    ]
    ordered_checks = first_pass + second_pass + third_pass + fourth_pass

    for u in ordered_checks:
        ok, extra, agg_url = verify_twitter_and_enrich(u, site_domain)
        if ok:
            twitter_final = u
            enriched_from_agg = extra or {}
            aggregator_url = agg_url or ""

            _VERIFIED_TW_URL = twitter_final
            _VERIFIED_ENRICHED = dict(enriched_from_agg)
            _VERIFIED_AGG_URL = aggregator_url
            _VERIFIED_DOMAIN = (site_domain or "").lower()

            avatar_url = ""
            try:
                prof = get_links_from_x_profile(twitter_final, need_avatar=True)
                avatar_url = (prof or {}).get("avatar", "") or ""
            except Exception:
                avatar_url = ""

            logger.info("X подтвержден: %s", twitter_final)
            return twitter_final, enriched_from_agg, aggregator_url, avatar_url

    # единственный профиль с аватаром
    if len(deduped) == 1:
        sole = deduped[0]
        try:
            prof = get_links_from_x_profile(sole, need_avatar=True)
            if isinstance(prof, dict) and (prof.get("avatar") or "").strip():
                _VERIFIED_TW_URL = twitter_final = sole
                _VERIFIED_ENRICHED = {}
                _VERIFIED_AGG_URL = ""
                logger.info("X подтвержден: %s", twitter_final)
                return twitter_final, {}, "", ""
        except Exception:
            pass

    # фолбэк: брендоподобный без верификации
    brand_like = []
    for u in deduped:
        if _handle_contains_brand(u):
            brand_like.append(u)

    twitter_final = brand_like[0] if brand_like else ""
    if twitter_final:
        # фиксируем как "подтвержденный" в рамках текущего домена
        _VERIFIED_TW_URL = twitter_final
        _VERIFIED_ENRICHED = dict(enriched_from_agg or {})
        _VERIFIED_AGG_URL = aggregator_url or ""
        _VERIFIED_DOMAIN = (site_domain or "").lower()

        # один заход в профиль для аватарки (через кэш, без лишнего Playwright)
        avatar_url = ""
        try:
            prof = get_links_from_x_profile(twitter_final, need_avatar=True)
            avatar_url = (prof or {}).get("avatar", "") or ""
        except Exception:
            avatar_url = ""

        logger.info("X подтвержден (fallback, handle≈brand): %s", twitter_final)
        return twitter_final, enriched_from_agg, aggregator_url, avatar_url
    else:
        logger.info("Фолбэк: брендоподобных X не найден — twitterURL пустой.")

    return twitter_final, enriched_from_agg, aggregator_url, ""


# Функция: скачать аватар X-профиля и сохранить в storage_dir/filename
def download_twitter_avatar(
    avatar_url: str | None,
    twitter_url: str | None,
    storage_dir: str,
    filename: str,
) -> str | None:
    if not storage_dir:
        logger.warning(
            "download_twitter_avatar: storage_path пуст - некуда сохранять аватар"
        )
        return None
    if not twitter_url:
        logger.warning(
            "download_twitter_avatar: twitterURL отсутствует - пропуск скачивания аватара"
        )
        return None

    # попытка получить avatar_url из профиля
    if not avatar_url:
        try:
            prof = get_links_from_x_profile(twitter_url, need_avatar=True)
            avatar_url = prof.get("avatar", "") if isinstance(prof, dict) else ""
            if avatar_url:
                logger.info("Avatar URL (подтвержден): %s", avatar_url)
        except Exception as e:
            logger.warning(
                "download_twitter_avatar: не удалось получить avatar из профиля: %s", e
            )
            avatar_url = ""

    if not avatar_url:
        logger.warning(
            "download_twitter_avatar: avatar пуст - ни Nitter/HTTP /photo, ни Playwright не дали URL для %s",
            twitter_url,
        )
        return None

    avatar_url_raw = normalize_twitter_avatar(force_https(avatar_url))
    logger.info("Avatar URL: %s", avatar_url_raw)

    headers_img = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*;q=0.8,*/*;q=0.5",
        "Referer": twitter_url or "https://x.com/",
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
                    url_img, timeout=timeout, headers=headers, allow_redirects=True
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

    resp_img = _get_image_with_retry(avatar_url_raw, headers_img, tries=3, timeout=25)
    if not resp_img:
        logger.warning("Не скачан: нет ответа от сервера, url=%s", avatar_url_raw)
        return None

    ct = (resp_img.headers.get("Content-Type") or "").lower()
    if not (
        resp_img.status_code == 200
        and resp_img.content
        and ("image/" in ct or avatar_url_raw.startswith("https://pbs.twimg.com/"))
    ):
        logger.warning(
            "Не скачан: code=%s, ct=%s, url=%s",
            getattr(resp_img, "status_code", "no-response"),
            ct,
            avatar_url_raw,
        )
        return None

    os.makedirs(storage_dir, exist_ok=True)
    avatar_path = os.path.join(storage_dir, filename)
    try:
        with open(avatar_path, "wb") as imgf:
            imgf.write(resp_img.content)
        logger.info("Сохранен: %s", avatar_path)
        return os.path.abspath(avatar_path)
    except Exception as e:
        logger.warning("Ошибка записи файла аватара: %s", e)
        return None


# Функция: сбросить зафиксированное состояние верификации и кэши профилей
def reset_verified_state(full: bool = False) -> None:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL, _VERIFIED_DOMAIN
    _VERIFIED_TW_URL = ""
    _VERIFIED_ENRICHED = {}
    _VERIFIED_AGG_URL = ""
    _VERIFIED_DOMAIN = ""

    if full:
        try:
            _PARSED_X_PROFILE_CACHE.clear()
        except Exception:
            pass


# Алиасы (для обратной совместимости с более старым кодом)
_extract_twitter_profiles = extract_twitter_profiles
_extract_link_collection_urls = extract_link_collection_urls
_is_valid_x_profile = _is_valid_x_profile
_verify_twitter_and_enrich = verify_twitter_and_enrich
_guess_twitter_handles = guess_twitter_handles
_fetch_and_extract_twitter_profiles = fetch_and_extract_twitter_profiles
_pick_best_twitter = pick_best_twitter

# Экспорт хоста для других модулей
host = _host
