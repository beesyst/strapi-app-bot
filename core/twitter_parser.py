from __future__ import annotations

import json
import os
import random
import re
import subprocess
from time import time as now
from typing import Dict, List, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_utils import get_logger

logger = get_logger("twitter_parser")


# Утилиты протокола/хостов
def force_https(url: str) -> str:
    if not url or not isinstance(url, str):
        return url
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Конфиг
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")

LINK_COLLECTION_DOMAINS = set()
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _cfg = json.load(f)

    LINK_COLLECTION_DOMAINS = {
        (d or "").lower().replace("www.", "")
        for d in _cfg.get("link_collections", [])
        if d
    }

    _nitter_cfg = _cfg.get("nitter", {}) or {}
    NITTER_ENABLED = bool(_nitter_cfg.get("nitter_enabled", True))
    NITTER_INSTANCES = [
        force_https(str(x).strip().rstrip("/"))
        for x in (_nitter_cfg.get("nitter_instances") or [])
        if x
    ]
    NITTER_RETRY_PER_INSTANCE = int(_nitter_cfg.get("nitter_retry_per_instance", 1))
    NITTER_TIMEOUT_SEC = int(_nitter_cfg.get("nitter_timeout_sec", 14))
    NITTER_BAD_TTL_SEC = int(_nitter_cfg.get("nitter_bad_ttl_sec", 600))

except Exception as e:
    logger.warning("Ошибка чтения конфигурации: %s", e)
    LINK_COLLECTION_DOMAINS = set()
    NITTER_ENABLED = True
    NITTER_INSTANCES = []
    NITTER_RETRY_PER_INSTANCE = 1
    NITTER_TIMEOUT_SEC = 14
    NITTER_BAD_TTL_SEC = 600

# Кэш для parsed X профилей
_PARSED_X_PROFILE_CACHE: Dict[str, Dict] = {}

# Баны инстансов: {instance_url: unban_ts}
_NITTER_BAD: Dict[str, float] = {}


def _normalize_instance(u: str) -> str:
    return force_https((u or "").strip().rstrip("/"))


def _alive_nitter_instances() -> List[str]:
    if not NITTER_INSTANCES:
        return []
    t = now()
    alive = []
    for inst in NITTER_INSTANCES:
        inst = _normalize_instance(inst)
        if not inst:
            continue
        if _NITTER_BAD.get(inst, 0) > t:
            continue
        alive.append(inst)
    return alive or [_normalize_instance(NITTER_INSTANCES[0])]


def _ban_instance(inst: str):
    _NITTER_BAD[_normalize_instance(inst)] = now() + max(60, NITTER_BAD_TTL_SEC)


# Возврат (HTML, инстанс) профиля через Nitter-инстансы
def _fetch_nitter_profile_html(handle: str) -> Tuple[str, str]:
    if not handle:
        return "", ""

    instances = _alive_nitter_instances()
    random.shuffle(instances)

    headers = {"User-Agent": "Mozilla/5.0"}
    last_err = None

    for inst in instances:
        url = f"{inst.rstrip('/')}/{handle}"
        for attempt in range(max(1, NITTER_RETRY_PER_INSTANCE)):
            try:
                r = requests.get(
                    url,
                    headers=headers,
                    timeout=NITTER_TIMEOUT_SEC,
                    allow_redirects=True,
                )
                code = r.status_code
                if code == 200 and r.text:
                    logger.info("Nitter: HTTP 200 для %s", url)
                    return r.text, inst
                if code in (429, 502, 503, 504) or code >= 500 or not r.text:
                    logger.warning("Nitter: HTTP %s для %s", code, url)
                    _ban_instance(inst)
                    last_err = f"HTTP {code}"
                    break
                if code == 404:
                    logger.warning("Nitter: 404 для %s", url)
                    return "", inst
                last_err = f"HTTP {code}"
            except Exception as e:
                logger.warning(
                    "Nitter: ошибка запроса %s (attempt %s): %s", url, attempt + 1, e
                )
                last_err = str(e)
                _ban_instance(inst)
                break

    if last_err:
        logger.warning("Nitter: все инстансы не дали HTML (last=%s)", last_err)
    return "", ""


# Вспомогательные парсеры
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


# Простой загрузчик HTML
def _fetch_html(url: str, timeout: int = 30) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return r.text or ""
    except Exception as e:
        logger.warning("fetch_html error %s: %s", url, e)
        return ""


# Профиль: Nitter - Playwright
def get_links_from_x_profile(
    profile_url: str, need_avatar: bool = True
) -> Dict[str, object]:
    script_path = os.path.join(ROOT_DIR, "core", "twitter_parser.js")
    orig_url = (profile_url or "").strip()
    if not orig_url:
        return {"links": [], "avatar": "", "name": ""}

    safe_url = normalize_twitter_url(orig_url)

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
        else:
            logger.info("Кэш без аватара, дообогащаем: %s", safe_url)

    # nitter
    parsed = _parse_nitter_profile(safe_url)

    # playwright
    use_playwright = False
    if need_avatar and (
        not (isinstance(parsed, dict) and (parsed.get("avatar") or "").strip())
    ):
        use_playwright = True

    if use_playwright:

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

        had_nitter = bool(
            isinstance(parsed, dict)
            and (parsed.get("links") or parsed.get("name") or parsed.get("avatar"))
        )
        tries = [safe_url, safe_url + "/photo"]
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
                if isinstance(parsed, dict) and parsed:
                    parsed = {
                        "links": parsed.get("links") or data.get("links") or [],
                        "avatar": data.get("avatar") or parsed.get("avatar") or "",
                        "name": parsed.get("name") or data.get("name") or "",
                    }
                else:
                    parsed = data
                logger.info(
                    "X-профиль распарсен через Playwright%s: %s",
                    " (fallback после Nitter)" if had_nitter else "",
                    safe_url,
                )
                break

    if not isinstance(parsed, dict):
        parsed = {}

    cleaned = {
        "links": parsed.get("links") or [],
        "avatar": parsed.get("avatar") or "",
        "name": parsed.get("name") or "",
    }
    _PARSED_X_PROFILE_CACHE[safe_url] = cleaned
    return cleaned


def _verify_aggregator_belongs(
    agg_url: str, site_domain: str, twitter_handle: str
) -> Tuple[bool, dict]:
    try:
        html = _fetch_html(agg_url)
        if not html:
            return False, {}

        socials = _extract_socials_from_html(html, agg_url)

        # сайт совпадает по домену
        site_match = any(_host(v).endswith(site_domain) for v in socials.values() if v)

        # обратная ссылка на тот же твиттер-хэндл
        handle = (twitter_handle or "").lower()
        same_x = False
        if handle:
            p = re.compile(
                rf"https?://(?:www\.)?(?:x\.com|twitter\.com)/{re.escape(handle)}(?:/|$)",
                re.I,
            )
            if p.search(html):
                same_x = True

        ok = bool(site_match or same_x)
        if ok:
            enriched = {}
            for k, v in socials.items():
                if not v:
                    continue
                if _host(v) in LINK_COLLECTION_DOMAINS:
                    continue
                enriched[k] = v
            return True, enriched
        return False, {}
    except Exception as e:
        logger.warning("verify aggregator error for %s: %s", agg_url, e)
        return False, {}


# Парс профиль через nitter.net/<handle> без JS
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
        if not handle:
            return {}

        html, inst_used = _fetch_nitter_profile_html(handle)
        if not html:
            return {}

        soup = BeautifulSoup(html, "html.parser")

        # имя
        name_tag = soup.select_one(".profile-card .profile-name-full")
        name = (name_tag.get_text(strip=True) if name_tag else "") or ""

        # ава (img.avatar) + fallback из <meta property="og:image">
        avatar = ""

        # <a class="profile-card-avatar"><img ...></a> или старый .profile-card img.avatar
        img = soup.select_one(
            ".profile-card a.profile-card-avatar img, "
            "a.profile-card-avatar img, "
            ".profile-card img.avatar"
        )
        if img and img.get("src"):
            src = img["src"]
            if src.startswith("/pic/"):
                avatar = src[len("/pic/") :]
                if avatar and not avatar.startswith("http"):
                    avatar = "https://" + avatar.lstrip("/")
            elif src.startswith("http"):
                avatar = src

        # общий fallback: любой <img> c pbs.twimg.com/profile_images/
        if not avatar:
            any_img = soup.select_one("img[src*='pbs.twimg.com/profile_images/']")
            if any_img and any_img.get("src"):
                src = any_img["src"]
                if src.startswith("/pic/"):
                    avatar = src[len("/pic/") :]
                    if avatar and not avatar.startswith("http"):
                        avatar = "https://" + avatar.lstrip("/")
                elif src.startswith("http"):
                    avatar = src

        # og:image
        if not avatar:
            meta = soup.select_one("meta[property='og:image'], meta[name='og:image']")
            og = (meta.get("content") or "") if meta else ""
            if "pbs.twimg.com" in og:
                avatar = og

        # подчистка
        if avatar:
            avatar = avatar.replace("&amp;", "&")

        # ссылки из BIO/Website (+ нормализация nitter redirect'ов)
        links = set()
        for a in soup.select(".profile-bio a, .profile-website a, a[href]"):
            href = a.get("href", "") or ""
            if href.startswith("/url/"):
                href = href[len("/url/") :]
            if href.startswith("/"):
                continue
            if href.startswith("http"):
                links.add(force_https(href))

        # лог-источник
        logger.info(
            "X-профиль распарсен через Nitter: %s (инстанс: %s)",
            f"https://x.com/{handle}",
            inst_used or "-",
        )

        return {"links": list(links), "avatar": avatar, "name": name}
    except Exception as e:
        logger.warning("Nitter fallback error: %s", e)
        return {}


# Хелпер: совпадение бренда по хэндлу
def _relaxed_contains_brand(handle: str, brand_token: str) -> bool:
    h = re.sub(r"[^a-z0-9]", "", (handle or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (brand_token or "").lower())
    return bool(h and b and b in h)


# Работа с профилями
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

    # "голые" упоминания в тексте/скриптах
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
        logger.info("Найдено X‑профилей на %s: %d", base_url, len(out))
    return out


# Возврат всех ссылок на link-агрегаторы (linktr.ee, link3.to, …)
def extract_link_collection_urls(html: str, base_url: str) -> List[str]:
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


# Фильтр "пустых"/новых профилей X (без авы, без имени, без ссылок)
def _is_valid_x_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    name = (parsed.get("name") or "").strip().lower()
    avatar = (parsed.get("avatar") or "").strip()
    links = parsed.get("links") or []
    if (not avatar) and (not name or "new to x" in name) and (not links):
        return False
    return True


# Соц-линки из HTML
def _extract_socials_from_html(html: str, base_url: str) -> dict:
    SOCIAL_PATTERNS = {
        "twitterURL": re.compile(r"twitter\.com|x\.com", re.I),
        "discordURL": re.compile(r"discord\.gg|discord\.com", re.I),
        "telegramURL": re.compile(r"t\.me|telegram\.me", re.I),
        "youtubeURL": re.compile(r"youtube\.com|youtu\.be", re.I),
        "linkedinURL": re.compile(r"linkedin\.com", re.I),
        "redditURL": re.compile(r"reddit\.com", re.I),
        "mediumURL": re.compile(r"medium\.com", re.I),
        "githubURL": re.compile(r"github\.com", re.I),
        "websiteURL": re.compile(r"^https?://", re.I),
    }
    soup = BeautifulSoup(html or "", "html.parser")
    out = {k: "" for k in SOCIAL_PATTERNS}
    for a in soup.find_all("a", href=True):
        abs_href = urljoin(base_url, a["href"])
        for key, pattern in SOCIAL_PATTERNS.items():
            if pattern.search(abs_href):
                out[key] = force_https(abs_href)
    out["websiteURL"] = out.get("websiteURL") or force_https(base_url)
    return out


# Проверка twitter_url
def verify_twitter_and_enrich(twitter_url: str, site_domain: str) -> Tuple[bool, dict]:
    data = get_links_from_x_profile(twitter_url, need_avatar=False)
    if not _is_valid_x_profile(data):
        return False, {}

    # handle для обратной проверки на агрегаторе
    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
        (twitter_url or "") + "/",
        re.I,
    )
    handle = m.group(1) if m else ""

    bio_links = [force_https(b) for b in (data.get("links") or [])]

    # прямой сайт - мгновенная верификация
    for b in bio_links:
        try:
            if _host(b).endswith(site_domain):
                return True, {}
        except Exception:
            pass

    # линк-агрегаторы - подтверждение принадлежности
    for b in bio_links:
        if _host(b) in LINK_COLLECTION_DOMAINS:
            ok, enriched = _verify_aggregator_belongs(b, site_domain, handle)
            if ok:
                return True, enriched

    return False, {}


# Генерация кандидатов хэндлов по бренду (<=15 символов)
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


# Утилита: загрузка страницы и парс всех X-профилей
def fetch_and_extract_twitter_profiles(from_url: str) -> List[str]:
    try:
        html = _fetch_html(from_url)
        return extract_twitter_profiles(html, from_url)
    except Exception:
        return []


# Выбор "лучшего" X-профиля
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


# Аватары
def normalize_twitter_avatar(url: str) -> str:
    u = force_https(url or "")

    # nitter: /pic/https://pbs.twimg.com/...
    if u.startswith("/pic/"):
        u = "https://" + u[len("/pic/") :].lstrip("/")
    # safety
    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u

    # нормализация размер до 400x400
    u = re.sub(
        r"(_normal|_bigger|_mini|_\d{2,4}x\d{2,4})\.(jpg|jpeg|png|webp)(\?.*)?$",
        r"_400x400.\2",
        u,
        flags=re.I,
    )
    # сброс query/fragment
    u = re.sub(r"(\.(?:jpg|jpeg|png|webp))(?:\?[^#]*)?(?:#.*)?$", r"\1", u, flags=re.I)
    return u


def select_verified_twitter(
    found_socials: dict,
    socials: dict,
    site_domain: str,
    brand_token: str,
    html: str,
    url: str,
    max_internal_links: int = 10,
    trust_home: bool = False,
) -> tuple[str, dict]:
    twitter_final = ""
    enriched_from_agg = {}

    # домашний X (верификация)
    if found_socials.get("twitterURL"):
        twitter_final, enriched_from_agg, _verified = decide_home_twitter(
            home_twitter_url=found_socials["twitterURL"],
            site_domain=site_domain,
            trust_home=trust_home,
        )

    # кандидаты из browser_fetch.js (все X‑ссылки из рендеренного DOM)
    browser_twitter_ordered = []
    if isinstance(socials, dict) and isinstance(socials.get("twitterAll"), list):
        browser_twitter_ordered = [
            u for u in socials["twitterAll"] if isinstance(u, str) and u
        ]

    # если итогового X нет - сбор альтернативы
    if not twitter_final:
        candidates_ordered = []

        # DOM‑кандидаты
        candidates_ordered.extend(browser_twitter_ordered)

        # сырой HTML главной
        candidates_ordered.extend(_extract_twitter_profiles(html, url))

        # импорты отложенные
        from core.web_parser import fetch_url_html, get_internal_links

        # внутренние страницы (до 5)
        for link in get_internal_links(
            html, url, max_links=min(max_internal_links, 10)
        )[:5]:
            try:
                page_html = fetch_url_html(link)
                candidates_ordered.extend(_extract_twitter_profiles(page_html, link))
            except Exception:
                pass

        # docs
        docs_html = ""
        docs_url_local = found_socials.get("documentURL")
        if docs_url_local:
            try:
                docs_html = fetch_url_html(docs_url_local)
                candidates_ordered.extend(
                    _extract_twitter_profiles(docs_html, docs_url_local)
                )
            except Exception:
                docs_html = ""

        # линк‑агрегаторы со страницы
        for coll_url in _extract_link_collection_urls(html, url):
            try:
                c_html = fetch_url_html(coll_url)
                candidates_ordered.extend(_extract_twitter_profiles(c_html, coll_url))
            except Exception:
                pass

        # линк‑агрегаторы из docs
        if docs_url_local and docs_html:
            for coll_url in _extract_link_collection_urls(docs_html, docs_url_local):
                try:
                    c_html = fetch_url_html(coll_url)
                    candidates_ordered.extend(
                        _extract_twitter_profiles(c_html, coll_url)
                    )
                except Exception:
                    pass

        # дедуп с сохранением порядка и нормализацией
        seen = set()
        deduped = []
        for u in candidates_ordered:
            if not isinstance(u, str) or not u:
                continue
            u_norm = normalize_twitter_url(u)
            if u_norm not in seen:
                deduped.append(u_norm)
                seen.add(u_norm)

        # хелпер: "хэндл содержит brand_token"
        def _handle_contains_brand(u: str) -> bool:
            m = re.match(
                r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
                (u or "") + "/",
                re.I,
            )
            h = m.group(1).lower() if m else ""
            return bool(brand_token and h and brand_token.lower() in h)

        dom_set = {normalize_twitter_url(u) for u in browser_twitter_ordered}

        # прио
        first_pass = [u for u in deduped if u in dom_set and _handle_contains_brand(u)]
        second_pass = [
            u for u in deduped if u in dom_set and not _handle_contains_brand(u)
        ]
        third_pass = [
            u for u in deduped if u not in dom_set and _handle_contains_brand(u)
        ]
        fourth_pass = [
            u for u in deduped if u not in dom_set and not _handle_contains_brand(u)
        ]
        ordered_checks = first_pass + second_pass + third_pass + fourth_pass

        # верификация
        for u in ordered_checks:
            ok, extra = _verify_twitter_and_enrich(u, site_domain)
            if ok:
                twitter_final = u
                enriched_from_agg = extra
                break

        # фолбек
        if not twitter_final:
            brand_like = []
            for u in deduped:
                m = re.match(
                    r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
                    (u or "") + "/",
                    re.I,
                )
                handle = m.group(1) if m else ""
                if _relaxed_contains_brand(handle, brand_token):
                    brand_like.append(u)

            twitter_final = brand_like[0] if brand_like else ""

            if twitter_final:
                logger.info(
                    "Фолбэк: выбран брендоподобный X без верификации: %s", twitter_final
                )
            else:
                logger.info(
                    "Фолбэк: брендоподобных X не найдено — оставляю twitterURL пустым."
                )

    return twitter_final, enriched_from_agg


# Нормализация url
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


# Возврат верифицированного X-профиля
def decide_home_twitter(
    home_twitter_url: str, site_domain: str, trust_home: bool = True
):
    if not home_twitter_url:
        return "", {}, False

    ok, extra = verify_twitter_and_enrich(home_twitter_url, site_domain)
    norm = normalize_twitter_url(home_twitter_url)

    if ok:
        logger.info("Домашний X‑профиль верифицирован: %s", norm)
        return norm, (extra or {}), True

    logger.info(
        "Домашний X‑профиль не верифицирован (игнор, даже при trust_home=%s): %s",
        bool(trust_home),
        norm,
    )
    return "", {}, False


# Загрузка авы
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
            "download_twitter_avatar: twitterURL отсутствует — пропускаю скачивание аватара"
        )
        return None

    # если аватар не передан - попытка получить из профиля
    if not avatar_url:
        try:
            prof = get_links_from_x_profile(twitter_url, need_avatar=True)
            avatar_url = prof.get("avatar", "") if isinstance(prof, dict) else ""
        except Exception as e:
            logger.warning(
                "download_twitter_avatar: не удалось получить avatar из профиля: %s", e
            )
            avatar_url = ""

    if not avatar_url:
        logger.warning(
            "download_twitter_avatar: avatar пуст - ни Nitter, ни Playwright не дали URL для %s",
            twitter_url,
        )
        return None

    # нормализация url и параметры запроса
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


# Алиасы
_extract_twitter_profiles = extract_twitter_profiles
_extract_link_collection_urls = extract_link_collection_urls
_is_valid_x_profile = _is_valid_x_profile
_verify_twitter_and_enrich = verify_twitter_and_enrich
_guess_twitter_handles = guess_twitter_handles
_fetch_and_extract_twitter_profiles = fetch_and_extract_twitter_profiles
_pick_best_twitter = pick_best_twitter

# Экспорт хоста
host = _host
