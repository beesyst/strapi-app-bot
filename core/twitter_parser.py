from __future__ import annotations

import json
import os
import random
import re
import subprocess
from time import time as now
from typing import Dict, List, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger
from core.parser_link_aggregator import (
    find_aggregators_in_links as _find_aggs_in_links,
)
from core.parser_link_aggregator import (
    is_link_aggregator,
)
from core.parser_link_aggregator import (
    verify_aggregator_belongs as _verify_agg_belongs,
)
from core.parser_web import fetch_url_html

logger = get_logger("twitter_parser")


# Нижний регистр и только [a-z0-9]
def _norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _handle_from_url(u: str) -> str:
    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (u or "") + "/", re.I
    )
    return m.group(1) if m else ""


# Строгий матч
def _strict_site_match_handle(brand_token: str, handle: str) -> bool:
    return (brand_token or "").lower() == (handle or "").lower()


# Нормализация протокола в https и обрезка мусора
def force_https(url: str) -> str:
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Возврат host без www
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Канонизация X-профиля: twitter.com -> x.com, срез query/fragment/хвостов
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
    return f"https://x.com/{m.group(1)}" if m else u.rstrip("/")


# Нормализация url аватарки
def normalize_twitter_avatar(url: str) -> str:
    u = force_https(url or "")
    if not u:
        return ""

    # nitter-перенаправления и проценты
    if u.startswith("/pic/") or "%2F" in u or "%3A" in u:
        u = _decode_nitter_pic_url(u)

    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u

    # убрать query/fragment
    u = re.sub(r"(?:\?[^#]*)?(?:#.*)?$", "", u)
    return u


# Декодер /pic/pbs.twimg.com%2F... или /pic/https%3A%2F%2Fpbs... в нормальный https
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


# Конфиг
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")

try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _cfg = json.load(f)

    # nitter
    _nitter_cfg = _cfg.get("nitter", {})
    for key in (
        "nitter_enabled",
        "nitter_instances",
        "nitter_retry_per_instance",
        "nitter_timeout_sec",
        "nitter_bad_ttl_sec",
    ):
        if key not in _nitter_cfg:
            raise KeyError(f"Отсутствует ключ config.json:nitter -> {key}")

    NITTER_ENABLED = bool(_nitter_cfg["nitter_enabled"])
    NITTER_INSTANCES = [
        force_https(str(x).strip().rstrip("/"))
        for x in (_nitter_cfg["nitter_instances"] or [])
        if x
    ]
    NITTER_RETRY_PER_INSTANCE = int(_nitter_cfg["nitter_retry_per_instance"])
    NITTER_TIMEOUT_SEC = int(_nitter_cfg["nitter_timeout_sec"])
    NITTER_BAD_TTL_SEC = int(_nitter_cfg["nitter_bad_ttl_sec"])

    # Playwright
    TW_PLAYWRIGHT_ENABLED = True
    TW_PLAYWRIGHT_IF_NITTER_FAILED_ONLY = True

except Exception:
    logger.exception("Не удалось прочитать конфиг из %s", CONFIG_PATH)
    raise

# Кэш
_PARSED_X_PROFILE_CACHE: Dict[str, Dict] = (
    {}
)  # {profile_url: {"links":[], "avatar":"", "name":""}}
_NITTER_HTML_CACHE: Dict[str, Tuple[str, str]] = {}
_NITTER_BAD: Dict[str, float] = {}


# Nitter
def _normalize_instance(u: str) -> str:
    return force_https((u or "").strip().rstrip("/"))


# Возврат списка живых nitter-инстансов с учетом банов
def _alive_nitter_instances() -> List[str]:
    if not NITTER_INSTANCES:
        return []
    t = now()
    alive = []
    for inst in NITTER_INSTANCES:
        inst = _normalize_instance(inst)
        if inst and _NITTER_BAD.get(inst, 0) <= t:
            alive.append(inst)
    return alive


# Бан инстанса на время
def _ban_instance(inst: str):
    _NITTER_BAD[_normalize_instance(inst)] = now() + max(60, NITTER_BAD_TTL_SEC)


# Загрузка html профиля через nitter, с кэшем и одноразовым логом http 200
def _fetch_nitter_profile_html(handle: str) -> Tuple[str, str]:
    if not handle:
        return "", ""
    handle_lc = (handle or "").lower()

    cached = _NITTER_HTML_CACHE.get(handle_lc)
    if cached:
        return cached

    instances = _alive_nitter_instances()
    random.shuffle(instances)

    last_err = None
    script_path = os.path.join(ROOT_DIR, "core", "browser_fetch.js")

    def _run_instance(inst_url: str):
        try:
            u = f"{inst_url.rstrip('/')}/{handle}"
            # raw-режим: просим чистый HTML и статус
            return subprocess.run(
                ["node", script_path, u, "--raw"],
                cwd=os.path.dirname(script_path),
                capture_output=True,
                text=True,
                timeout=max(NITTER_TIMEOUT_SEC + 6, 20),
            )
        except Exception as e:
            logger.warning("Nitter fetch runner failed for %s: %s", inst_url, e)
            return None

    for inst in instances:
        for attempt in range(max(1, NITTER_RETRY_PER_INSTANCE)):
            res = _run_instance(inst)
            if not res:
                _ban_instance(inst)
                last_err = "runner_failed"
                break
            stdout = res.stdout or ""
            stderr = res.stderr or ""

            # json: { ok, html, status, antiBot: {detected, kind}, instance }
            try:
                data = json.loads(stdout) if stdout.strip().startswith("{") else {}
            except Exception:
                data = {}

            if (
                isinstance(data, dict)
                and data.get("ok")
                and (data.get("html") or "").strip()
            ):
                html = data.get("html", "")
                _NITTER_HTML_CACHE[handle_lc] = (html, inst)
                return html, inst

            kind = (data.get("antiBot") or {}).get("kind", "")
            status = data.get("status", 0)
            if kind or status in (403, 429, 503, 0):
                _ban_instance(inst)
            last_err = kind or f"HTTP {status}" or "no_html"

    if last_err:
        logger.warning("Nitter: все инстансы не дали HTML (last=%s)", last_err)
    return "", ""


# Url аватарки из nitter-страницы
def _pick_avatar_from_soup(soup: BeautifulSoup) -> str:
    img = soup.select_one(
        ".profile-card a.profile-card-avatar img, "
        "a.profile-card-avatar img, "
        ".profile-card img.avatar, "
        "img[src*='pbs.twimg.com/profile_images/']"
    )
    if img and img.get("src"):
        return _decode_nitter_pic_url(img["src"])

    a = soup.select_one(".profile-card a.profile-card-avatar[href]")
    if a and a.get("href"):
        return _decode_nitter_pic_url(a["href"])

    meta = soup.select_one("meta[property='og:image'], meta[name='og:image']")
    if meta and meta.get("content"):
        c = meta["content"]
        if "/pic/" in c or "%2F" in c or "%3A" in c:
            return _decode_nitter_pic_url(c)
        if "pbs.twimg.com" in c:
            return force_https(c)

    return ""


# Парс профиля через nitter (без JS): name, links, avatar
def _parse_nitter_profile(twitter_url: str) -> Dict[str, object]:
    try:
        if not NITTER_ENABLED:
            return {}

        m = re.match(
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
            (twitter_url or "") + "/",
            re.I,
        )
        handle = m.group(1) if m else ""
        handle_lc = handle.lower()
        if not handle:
            return {}

        # несколько инстансов, пока avatar или ссылка >=1
        max_rotations = max(1, len(_alive_nitter_instances()))
        attempt = 0
        best = {"links": [], "avatar": "", "name": ""}

        while attempt < max_rotations:
            html, inst_used = _fetch_nitter_profile_html(handle)
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")

            name_tag = soup.select_one(".profile-card .profile-name-full")
            name = (name_tag.get_text(strip=True) if name_tag else "") or ""

            links = set()
            # links = внешние http(s) из bio и website карточки (включая агрегаторы)
            for a in soup.select(".profile-bio a, .profile-website a"):
                href = a.get("href", "") or ""
                if href.startswith("/url/"):
                    href = href[len("/url/") :]
                if href.startswith("/"):
                    continue
                if href.startswith("http"):
                    links.add(force_https(href))

            avatar = _pick_avatar_from_soup(soup)

            # фолбэк: парс /pic/... из сырого html
            if not avatar:
                m_pic = re.search(
                    r"/pic/(?:https%3A%2F%2F|pbs\.twimg\.com%2F)[^\"'<> ]*profile_images[^\"'<> ]+\.(?:jpg|jpeg|png|webp)",
                    html,
                    re.I,
                )
                if m_pic:
                    avatar = _decode_nitter_pic_url(m_pic.group(0))

            clean_avatar = (
                normalize_twitter_avatar(avatar.replace("&amp;", "&")) if avatar else ""
            )

            # кэши сырого html
            if handle_lc not in _NITTER_HTML_CACHE:
                _NITTER_HTML_CACHE[handle_lc] = (html, inst_used or "")

            # единая строка лога: GET + parse
            logger.info(
                "Nitter GET+parse: %s/%s → avatar=%s, links=%d",
                (inst_used or "-").rstrip("/"),
                handle,
                "yes" if clean_avatar else "no",
                len(links),
            )

            # лучший результат
            best = {"links": list(links), "avatar": clean_avatar, "name": name}

            # приоритет - ава
            if not clean_avatar:
                if inst_used:
                    _ban_instance(inst_used)
                    _NITTER_HTML_CACHE.pop(handle_lc, None)
                attempt += 1
                continue

            return best

        return best

    except Exception as e:
        logger.warning("Nitter fallback error: %s", e)
        return {}


# Парс первый JSON-объект из stdout/stderr Node-скрипта
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


# Универсальный извлекатель: ссылки, имя, аватар.
def get_links_from_x_profile(
    profile_url: str, need_avatar: bool = True
) -> Dict[str, object]:
    script_path = os.path.join(ROOT_DIR, "core", "twitter_parser.js")
    orig_url = (profile_url or "").strip()
    if not orig_url:
        return {"links": [], "avatar": "", "name": ""}

    safe_url = normalize_twitter_url(orig_url)

    # кэш
    cached = _PARSED_X_PROFILE_CACHE.get(safe_url)
    if cached:
        has_avatar = bool((cached.get("avatar") or "").strip())
        if (not need_avatar) or has_avatar:
            logger.info(
                "X-профиль из кэша: %s (need_avatar=%s, avatar=%s)",
                safe_url,
                need_avatar,
                "yes" if has_avatar else "no",
            )
            return cached
        parsed = dict(cached)
    else:
        parsed = {}

    # nitter
    if NITTER_ENABLED and not parsed:
        parsed = _parse_nitter_profile(safe_url) or {}

    if parsed and (parsed.get("links") or parsed.get("name") or parsed.get("avatar")):
        cleaned = {
            "links": parsed.get("links") or [],
            "avatar": normalize_twitter_avatar(parsed.get("avatar") or ""),
            "name": parsed.get("name") or "",
        }
        _PARSED_X_PROFILE_CACHE[safe_url] = cleaned
        if not need_avatar:
            return cleaned

        # Если nitter дал аву — playwright не нужен
        if cleaned.get("avatar"):
            return cleaned

        # ава из кэша html через регекс
        m = re.match(
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", safe_url + "/", re.I
        )
        h = m.group(1).lower() if m else ""
        if h and h in _NITTER_HTML_CACHE:
            html_cached, _ = _NITTER_HTML_CACHE[h]
            m_img = re.search(
                r"/pic/(?:https%3A%2F%2F|pbs\.twimg\.com%2F)[^\"'<> ]*profile_images[^\"'<> ]+\.(?:jpg|jpeg|png|webp)",
                html_cached,
                re.I,
            )
            if m_img:
                cleaned["avatar"] = normalize_twitter_avatar(
                    _decode_nitter_pic_url(m_img.group(0))
                )
                _PARSED_X_PROFILE_CACHE[safe_url] = cleaned
                return cleaned

        should_run_playwright = TW_PLAYWRIGHT_ENABLED and (not cleaned.get("avatar"))
        if TW_PLAYWRIGHT_IF_NITTER_FAILED_ONLY and not should_run_playwright:
            return cleaned
        if not should_run_playwright:
            return cleaned

        # playwright runner (Node)
        def _run_once(u: str):
            try:
                return subprocess.run(
                    ["node", script_path, u],
                    cwd=os.path.dirname(script_path),
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except Exception as e:
                logger.warning("Ошибка запуска twitter_parser.js для %s: %s", u, e)
                return None

        tries = [safe_url]
        if need_avatar:
            tries.append(safe_url + "/photo")

        for u in tries:
            result = _run_once(u)
            if not result:
                continue
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            data = _extract_first_json_object(stdout) or _extract_first_json_object(
                stderr
            )

            if isinstance(data, dict) and (
                data.get("links") or data.get("avatar") or data.get("name")
            ):
                merged = {
                    "links": (parsed.get("links") if isinstance(parsed, dict) else [])
                    or (data.get("links") or []),
                    "avatar": normalize_twitter_avatar(
                        data.get("avatar")
                        or (parsed.get("avatar") if isinstance(parsed, dict) else "")
                        or ""
                    ),
                    "name": (parsed.get("name") if isinstance(parsed, dict) else "")
                    or (data.get("name") or ""),
                }
                _PARSED_X_PROFILE_CACHE[safe_url] = merged
                logger.info("X-профиль распарсен через Playwright: %s", safe_url)
                return merged

            # regex-фолбэк: парс pbs из stdout/stderr
            blob = (stdout or "") + "\n" + (stderr or "")
            m_img = re.search(
                r"https://pbs\.twimg\.com/profile_images/[^\s\"']+\.(?:jpg|jpeg|png|webp)",
                blob,
                re.I,
            )
            if m_img:
                merged = {
                    "links": (parsed.get("links") if isinstance(parsed, dict) else [])
                    or [],
                    "avatar": normalize_twitter_avatar(m_img.group(0)),
                    "name": (parsed.get("name") if isinstance(parsed, dict) else "")
                    or "",
                }
                _PARSED_X_PROFILE_CACHE[safe_url] = merged
                logger.info(
                    "X-профиль распарсен через Playwright (regex-fallback): %s",
                    safe_url,
                )
                return merged

    return _PARSED_X_PROFILE_CACHE.get(
        safe_url, {"links": [], "avatar": "", "name": ""}
    )


# Поиск X‑профили в html (из <a> и «голых» упоминаний)
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


# Поиск ссылок на линк‑агрегаторы на странице
def extract_link_collection_urls(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if is_link_aggregator(href):
            urls.append(force_https(href))
    return urls


# Профиль валиден, если не полностью пустой/"new to X" без ссылок и авы
def _is_valid_x_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    name = (parsed.get("name") or "").strip().lower()
    avatar = (parsed.get("avatar") or "").strip()
    links = parsed.get("links") or []
    if (not avatar) and (not name or "new to x" in name) and (not links):
        return False
    return True


# Проверка twitter_url по bio/агрегатору/сайту, возврат (ok, enriched_socials, agg_url)
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
            from core.parser_link_aggregator import extract_socials_from_aggregator

            socials_clean = extract_socials_from_aggregator(agg_url) or {}
            if site_domain:
                socials_clean["websiteURL"] = f"https://www.{site_domain}/"
            logger.info(
                "Агрегатор подтвержден (text-match) и дал соц-ссылки: %s",
                {k: v for k, v in socials_clean.items() if v},
            )
            return True, socials_clean, agg_url

    # прямой сайт в bio - ок
    for b in bio_links:
        try:
            if _host(b).endswith(site_domain):
                return True, {"websiteURL": f"https://www.{site_domain}/"}, ""
        except Exception:
            pass

    return False, {}, (aggs[0] if aggs else "")


# Возврат url сайта из агрегатора
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


# Загрузка страницы и извлечение X‑профилей
def fetch_and_extract_twitter_profiles(from_url: str) -> List[str]:
    try:
        html = fetch_url_html(from_url, prefer="auto", timeout=30)
        return extract_twitter_profiles(html, from_url)
    except Exception:
        return []


# Генерация возможных хэндлов по бренду (<=15 символов)
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


# Выбор лучшего X‑профиля по простому скорингу (с проверками bio и observed)
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


# Верификация "домашнего" X (из found_socials)
def decide_home_twitter(
    home_twitter_url: str, site_domain: str, trust_home: bool = True
):
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL
    if not home_twitter_url:
        return "", {}, False, ""

    ok, extra, agg_url = verify_twitter_and_enrich(home_twitter_url, site_domain)
    norm = normalize_twitter_url(home_twitter_url)

    if ok:
        logger.info("Домашний X‑профиль верифицирован: %s", norm)
        _VERIFIED_TW_URL = norm
        _VERIFIED_ENRICHED = dict(extra or {})
        _VERIFIED_AGG_URL = agg_url or ""
        return norm, (extra or {}), True, (agg_url or "")

    return "", {}, False, ""


# Поиск и фикс одного верифицированного X в рамках процесса
def select_verified_twitter(
    found_socials: dict,
    socials: dict,
    site_domain: str,
    brand_token: str,
    html: str,
    url: str,
    trust_home: bool = False,
) -> tuple[str, dict, str, str]:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL

    if _VERIFIED_TW_URL:
        return _VERIFIED_TW_URL, dict(_VERIFIED_ENRICHED), _VERIFIED_AGG_URL

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

    def _handle_from_url(u: str) -> str:
        m = re.match(
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (u or "") + "/", re.I
        )
        return (m.group(1) if m else "").lower()

    def _strict_site_match_handle(bt: str, h: str) -> bool:
        bt = (bt or "").lower().replace("-", "").replace("_", "")
        h2 = (h or "").lower().replace("-", "").replace("_", "")
        return bool(bt and h2 and (bt == h2 or h2.startswith(bt) or bt in h2))

    bt = (brand_token or "").lower()
    dom_set = {normalize_twitter_url(u) for u in browser_twitter_ordered}

    # хедер/футер/меню
    for u in deduped:
        if u in dom_set:
            h = _handle_from_url(u)
            if _strict_site_match_handle(bt, h):
                logger.info("X подтвержден по ссылке с сайта: %s", u)
            else:
                logger.info(
                    "X подтвержден по ссылке с сайта (handle ≠ brand_token, но доверяем сайту): %s",
                    u,
                )
            _VERIFIED_TW_URL = twitter_final = u
            _VERIFIED_ENRICHED = {}
            _VERIFIED_AGG_URL = ""
            return twitter_final, {}, "", (prof or {}).get("avatar", "") or ""

    # обычный порядок проверок (bio/агрегатор/скоринг)
    def _handle_contains_brand(u: str) -> bool:
        h = _handle_from_url(u)
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

            avatar_url = ""
            try:
                prof = get_links_from_x_profile(twitter_final, need_avatar=True)
                avatar_url = (prof or {}).get("avatar", "") or ""
            except Exception:
                avatar_url = ""

            logger.info(
                "X подтвержден: %s - дальнейшие проверки остановлены", twitter_final
            )
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
                logger.info(
                    "X подтвержден по единственному профилю с аватаром: %s",
                    twitter_final,
                )
                return twitter_final, {}, ""
        except Exception:
            pass

    # фолбэк: брендоподобный без верификации
    brand_like = []
    for u in deduped:
        if _handle_contains_brand(u):
            brand_like.append(u)

    twitter_final = brand_like[0] if brand_like else ""
    if twitter_final:
        logger.info(
            "Фолбэк: выбран брендоподобный X без верификации: %s", twitter_final
        )
    else:
        logger.info("Фолбэк: брендоподобных X не найден — twitterURL пустой.")

    return twitter_final, enriched_from_agg, aggregator_url, ""


# Скачивание авы
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

    # быстрый повтор
    if not avatar_url:
        try:
            prof2 = get_links_from_x_profile(twitter_url, need_avatar=True)
            avatar_url = prof2.get("avatar", "") if isinstance(prof2, dict) else ""
        except Exception:
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


# Сброс зафиксированного состояния верификации и кэшей
def reset_verified_state(full: bool = False) -> None:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL
    _VERIFIED_TW_URL = ""
    _VERIFIED_ENRICHED = {}
    _VERIFIED_AGG_URL = ""

    if full:
        try:
            _PARSED_X_PROFILE_CACHE.clear()
        except Exception:
            pass
        try:
            _NITTER_HTML_CACHE.clear()
        except Exception:
            pass
        try:
            _NITTER_BAD.clear()
        except Exception:
            pass


# Алиасы (для обратной совместимости)
_extract_twitter_profiles = extract_twitter_profiles
_extract_link_collection_urls = extract_link_collection_urls
_is_valid_x_profile = _is_valid_x_profile
_verify_twitter_and_enrich = verify_twitter_and_enrich
_guess_twitter_handles = guess_twitter_handles
_fetch_and_extract_twitter_profiles = fetch_and_extract_twitter_profiles
_pick_best_twitter = pick_best_twitter

# Экспорт хоста
host = _host
