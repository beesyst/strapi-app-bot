from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
from typing import Dict, List, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from core.log_utils import get_logger
from core.paths import PROJECT_ROOT
from core.settings import get_http_ua, get_settings

# Логгер для всего, что связано с Nitter (в логах будет [nitter])
logger = get_logger("nitter")

# Глобальные настройки и секция "nitter"
_SETTINGS = get_settings()
_n_cfg = _SETTINGS.get("nitter") or {}

# Один UA для всех запросов к Nitter в рамках процесса
_HTTP_UA_NITTER = get_http_ua()


# Вспомогательная функция: нормализовать URL к https и обрезать лишнее
def force_https(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Флаг включения/отключения Nitter
_ENABLED: bool = bool(_n_cfg.get("enabled", True))

# Нормализованный список инстансов (https, без завершающего /)
_INSTANCES: List[str] = []
for _u in _n_cfg.get("instances") or []:
    try:
        u = str(_u).strip()
        if not u:
            continue
        if u.startswith("http://"):
            u = "https://" + u[7:]
        elif not u.startswith("http"):
            u = "https://" + u.lstrip("/")
        u = u.rstrip("/")
        _INSTANCES.append(u)
    except Exception:
        continue

# Таймаут запроса к одному инстансу (сек)
_TIMEOUT: int = int(_n_cfg.get("timeout") or 15)

# Время бана “плохого” инстанса (сек)
_BAD_TTL: int = int(_n_cfg.get("bad_ttl") or 600)

# Максимальное число инстансов за один прогон
_MAX_INS: int = int(_n_cfg.get("max_ins") or 4)

# Стратегия выбора инстансов: random/round_robin
_STRATEGY: str = str(_n_cfg.get("strategy") or "random").lower()
if _STRATEGY not in ("random", "round_robin"):
    _STRATEGY = "random"

# Кэш HTML профиля: { handle_lc: (html, inst_base) }
_NITTER_HTML_CACHE: Dict[str, Tuple[str, str]] = {}

# Бан-лист: { inst_base: banned_until_timestamp }
_NITTER_BAD: Dict[str, float] = {}

# Состояние round-robin курсора
_RR_STATE = {"idx": 0}

# Счётчик попыток на один handle: { handle_lc: tries_count }
_HANDLE_TRIES: Dict[str, int] = {}


# Вспомогательная функция: список живых инстансов с учетом TTL-бана
def _alive_instances() -> List[str]:
    if not _INSTANCES:
        return []
    t = time.time()
    alive: List[str] = []
    for inst in _INSTANCES:
        inst_norm = force_https(inst).rstrip("/")
        if _NITTER_BAD.get(inst_norm, 0.0) <= t:
            alive.append(inst_norm)
    return alive


# Вспомогательная функция: забанить инстанс на BAD_TTL секунд
def _ban_instance(inst: str) -> None:
    base = force_https(inst).rstrip("/")
    _NITTER_BAD[base] = time.time() + max(60, _BAD_TTL)


# Вспомогательная функция: выбор инстансов с учетом стратегии (random/round_robin)
def _sample_instances(max_count: int) -> List[str]:
    alive = _alive_instances()
    if not alive:
        return []

    max_count = max(1, min(max_count, len(alive)))

    # round_robin
    if _STRATEGY == "round_robin":
        out: List[str] = []
        n = len(alive)
        start = _RR_STATE["idx"] % n
        i = start
        while len(out) < max_count:
            out.append(alive[i % n])
            i += 1
        _RR_STATE["idx"] = (start + len(out)) % n
        return out

    # random
    pool = alive[:]
    random.shuffle(pool)
    return pool[:max_count]


# Вспомогательная функция: декодировать /pic/... в https://pbs.twimg.com/...
def _decode_nitter_pic_url(src: str) -> str:
    if not src:
        return ""
    s = src.strip()
    if s.startswith("/pic/"):
        s = s[len("/pic/") :]

    s = unquote(s)

    # относительные пути к pbs.twimg.com
    if re.match(r"^/?(orig|media)/", s, re.I) and not s.startswith("http"):
        s = "https://pbs.twimg.com/" + s.lstrip("/")

    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("http://"):
        s = "https://" + s[7:]
    elif not s.startswith("https://"):
        s = "https://" + s.lstrip("/")

    return s


# Вспомогательная функция: нормализовать URL аватарки (декодировать /pic/, убрать query)
def _normalize_avatar(url: str | None) -> str:
    u = force_https(url or "")
    if not u:
        return ""
    try:
        p = urlparse(u)
        if "/pic/" in (p.path or ""):
            return _decode_nitter_pic_url(p.path)
    except Exception:
        pass

    if u.startswith("/pic/"):
        u = _decode_nitter_pic_url(u)
    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u

    u = re.sub(r"(?:\?[^#]*)?(?:#.*)?$", "", u)

    if "pbs.twimg.com/profile_images/" in u:
        u = re.sub(
            r"(/profile_images/[^/]+/[^/_]+)"
            r"(?:_[0-9]+x[0-9]+|_x[0-9]+|_normal|_bigger|_mini)"
            r"(\.[a-zA-Z0-9]+)$",
            r"\1\2",
            u,
        )

    return u


# Вспомогательная функция: эвристика - HTML похож на антибот/заглушку
def _looks_antibot(html: str) -> bool:
    low = (html or "").lower()

    # если есть очевидные элементы профиля/таймлайна - это нормальная страница
    if "tweet-body" in low or "timeline-item" in low or "profile-card" in low:
        return False

    if "nitter" in low and len(low) > 200:
        return False

    needles = (
        "captcha",
        "verify you are human",
        "are you human",
        "access denied",
        "rate limit",
        "please enable javascript",
        "just a moment",
        "checking your browser",
    )

    return any(s in low for s in needles) or len(low) < 200


# Вспомогательная функция: HTML действительно про нужный @handle?
def _html_matches_handle(html: str, handle: str) -> bool:
    if not html or not handle:
        return False
    low = html.lower()
    h = handle.lower()
    # href="/handle" в ссылке профиля
    if re.search(rf'href\s*=\s*["\']/\s*{re.escape(h)}(?:["\'/?# ]|$)', low):
        return True
    # @handle в тексте
    if re.search(rf"@{re.escape(h)}(?:[\"\' <]|$)", low):
        return True
    # profile-card + handle
    if "profile-card" in low and h in low:
        return True
    return False


# Вспомогательная функция: запуск browser_fetch.js в режиме raw для Nitter-URL
def _run_nitter_fetch(url: str, timeout_sec: int) -> tuple[str, int, str]:
    script_path = os.path.join(PROJECT_ROOT, "core", "parser", "browser_fetch.js")
    args = [
        "node",
        script_path,
        url,
        "--raw",
        "--ua",
        _HTTP_UA_NITTER,
        "--wait",
        "networkidle",
        "--retries",
        "2",
        "--scrollPages",
        "4",
        "--fp-device",
        "desktop",
        "--fp-os",
        "linux",
        "--fp-locales",
        "en-US,ru-RU",
        "--fp-viewport",
        "1366x768",
        "--nitter",
        "true",
    ]

    try:
        res = subprocess.run(
            args,
            cwd=os.path.dirname(script_path),
            capture_output=True,
            text=True,
            timeout=max(timeout_sec + 8, 25),
        )
    except Exception as e:
        logger.debug("nitter: ошибка запуска browser_fetch.js для %s: %s", url, e)
        return "", 0, "runner_failed"

    raw = (res.stdout or "").strip()
    try:
        data = json.loads(raw) if raw.startswith("{") else {}
    except Exception:
        data = {}

    if not isinstance(data, dict):
        return "", 0, "bad_payload"

    html = (data.get("html") or data.get("text") or "") or ""
    status = int(data.get("status", 0) or 0)
    kind = (data.get("antiBot") or {}).get("kind", "") or ""
    return html.strip(), status, kind


# Вспомогательная функция: найти именно карточку профиля нужного handle
def _find_profile_card(soup: BeautifulSoup, handle: str):
    if not soup or not handle:
        return None

    handle_lc = handle.lower().lstrip("@")

    cards = soup.select(".profile-card")
    primary: list = []
    fallback: list = []

    for c in cards:
        # не берем карточки из ленты/твитов
        if c.find_parent(class_="tweet-body") or c.find_parent(class_="timeline-item"):
            continue

        uname = c.select_one(".profile-card-username") or c.select_one(
            ".profile-username"
        )
        uname_text = (uname.get_text(strip=True) if uname else "") or ""
        uname_href = (uname.get("href") or "") if uname else ""

        # текстовый handle, например "@BuzzingApp" -> "buzzingapp"
        text_handle = uname_text.lower().lstrip("@")

        # handle из href="/BuzzingApp"
        href_handle = ""
        m = re.search(r"/([A-Za-z0-9_]{1,15})(?:$|[/?#])", uname_href)
        if m:
            href_handle = m.group(1).lower()

        # жесткое совпадение по профилю
        if text_handle == handle_lc or href_handle == handle_lc:
            primary.append(c)
        elif handle_lc in uname_text.lower() or handle_lc in uname_href.lower():
            fallback.append(c)

    if primary:
        return primary[0]
    if fallback:
        return fallback[0]

    # фолбэк: первый .profile-card, который не в ленте
    for c in cards:
        if not c.find_parent(class_="tweet-body") and not c.find_parent(
            class_="timeline-item"
        ):
            return c

    return None


# Вспомогательная функция: легкий парс BIO/аватарки для логов
def _probe_profile(
    html: str, inst_base: str, handle: str
) -> tuple[str, str, list[str]]:
    if not html:
        return "", "", []
    soup = BeautifulSoup(html, "html.parser")

    base_root = force_https(inst_base).rstrip("/")

    # ищем карточку именно нашего handle, с фильтрацией по ленте
    card = _find_profile_card(soup, handle)

    # если карточки профиля нет - ничего не берем из ленты, только попробуем аватар
    if not card:
        avatar_raw, avatar_norm = _pick_avatar_from_soup(soup, inst_base, handle)
        avatar_norm = _normalize_avatar(avatar_norm or "")
        return avatar_raw, avatar_norm, []

    # ссылки ищем только внутри карточки профиля, чтобы не лезть в ленту
    scope = card

    base = f"{base_root}/{handle.lower().lstrip('@')}"
    links, seen = set(), set()

    selectors = (
        ".profile-website a",
        ".profile-bio a",
    )

    for sel in selectors:
        for a in scope.select(sel):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            try:
                abs_u = urljoin(base, href)
            except Exception:
                abs_u = href

            if abs_u.startswith("//"):
                abs_u = "https:" + abs_u
            if not abs_u.startswith("http"):
                continue

            u = force_https(abs_u)

            try:
                host_u = urlparse(u).netloc.lower()
                host_inst = urlparse(base_root).netloc.lower()
                if host_u == host_inst:
                    continue
            except Exception:
                pass

            if u not in seen:
                seen.add(u)
                links.add(u)

    avatar_raw, avatar_norm = _pick_avatar_from_soup(soup, inst_base, handle)
    avatar_norm = _normalize_avatar(avatar_norm or "")

    return avatar_raw, avatar_norm, list(links)


# Вспомогательная функция: поиск аватарки в разметке профиля Nitter
def _pick_avatar_from_soup(
    soup: BeautifulSoup, inst_base: str, handle: str
) -> tuple[str, str]:
    base_root = force_https(inst_base).rstrip("/")

    # ищем карточку нашего handle (без чужих профилей из ленты)
    card = _find_profile_card(soup, handle)

    # если профиль не нашли, лучше вернуть пусто, чем аву другого аккаунта
    if not card:
        return "", ""

    search_root = card

    # <a class="profile-card-avatar" href="...">
    a = search_root.select_one(".profile-card a.profile-card-avatar[href]")
    if a and a.get("href"):
        href = (a.get("href") or "").strip()
        if href.startswith("/"):
            raw = f"{base_root}{href}"
        elif href.startswith("http"):
            raw = href
        else:
            raw = f"{base_root}/{href.lstrip('/')}"
        normalized = _decode_nitter_pic_url(href)
        return raw, normalized

    # <img ... class="avatar" ...>
    img = search_root.select_one(
        "a.profile-card-avatar img, "
        ".profile-card img.avatar, "
        "img[src*='pbs.twimg.com/profile_images/']"
    )
    if img and img.get("src"):
        src = (img.get("src") or "").strip()
        if src.startswith("/"):
            raw = f"{base_root}{src}"
        elif src.startswith("http"):
            raw = src
        else:
            raw = f"{base_root}/{src.lstrip('/')}"
        normalized = _decode_nitter_pic_url(src)
        return raw, normalized

    # <meta property="og:image">
    meta = search_root.select_one(
        "meta[property='og:image'], meta[name='og:image'], meta[property='twitter:image:src']"
    )
    if meta:
        c = (meta.get("content") or meta.attrs.get("content") or "").strip()
        if c:
            if "/pic/" in c or "%2F" in c or "%3A" in c:
                if c.startswith("/"):
                    raw = f"{base_root}{c}"
                elif c.startswith("http"):
                    raw = c
                else:
                    raw = f"{base_root}/{c.lstrip('/')}"
                normalized = _decode_nitter_pic_url(c)
                return raw, normalized
            if "pbs.twimg.com" in c:
                c2 = force_https(c)
                return c2, c2

    return "", ""


# Функция: сброс внутреннего состояния Nitter (кэш HTML, бан-лист, RR-курсор)
def reset_state() -> None:
    try:
        _NITTER_HTML_CACHE.clear()
    except Exception:
        pass
    try:
        _NITTER_BAD.clear()
    except Exception:
        pass
    try:
        _HANDLE_TRIES.clear()
    except Exception:
        pass
    _RR_STATE["idx"] = 0


# Функция: получить HTML профиля через Nitter (с логами и баном инстансов)
def fetch_profile_html(handle: str, probe_log: bool = True) -> tuple[str, str]:
    handle = (handle or "").strip()
    if not handle:
        return "", ""
    handle_lc = handle.lower()

    if not _ENABLED or not _INSTANCES:
        return "", ""

    # кэш на handle
    cached = _NITTER_HTML_CACHE.get(handle_lc)
    if cached:
        return cached

    # глобальный лимит попыток по handle
    used = _HANDLE_TRIES.get(handle_lc, 0)
    if used >= _MAX_INS:
        logger.debug(
            "nitter: лимит попыток (%s) для handle=%s уже исчерпан, Nitter пропускаем",
            _MAX_INS,
            handle,
        )
        return "", ""
    slots_left = max(1, _MAX_INS - used)

    last_err = "no_instances"
    candidates = _sample_instances(slots_left)

    # фиксируем, что мы уже попробовали эти инстансы для этого handle
    _HANDLE_TRIES[handle_lc] = used + len(candidates)

    for inst in candidates:
        base = force_https(inst).rstrip("/")
        url = f"{base}/{handle}"

        html, status, kind = _run_nitter_fetch(url, _TIMEOUT)
        if not html and not status and not kind:
            last_err = "no_html"
        else:
            last_err = kind or f"HTTP {status}" or "no_html"

        # лёгкий парс для логов (аватар, ссылки)
        if probe_log:
            avatar_raw, avatar_norm, links = _probe_profile(html, base, handle)
            try:
                logger.info(
                    "Nitter GET+parse: %s/%s → avatar=%s, links=%d",
                    base,
                    handle,
                    "yes" if (avatar_raw or avatar_norm) else "no",
                    len(links),
                )
                if links:
                    logger.info("BIO X (Nitter): %s", list(links))
            except Exception:
                pass

        # валидация HTML профиля
        if html and _html_matches_handle(html, handle) and not _looks_antibot(html):
            _NITTER_HTML_CACHE[handle_lc] = (html, base)
            return html, base

        # если явный антибот/ошибки - баним инстанс
        ban_reason = None

        if kind:
            ban_reason = f"antiBot={kind}"
        elif status in (403, 429, 503):
            ban_reason = f"status={status}"
        elif not html:
            ban_reason = "empty_html"

        if ban_reason:
            _ban_instance(base)
            logger.debug(
                "nitter: баним инстанс %s для handle=%s (reason=%s)",
                base,
                handle,
                ban_reason,
            )

    logger.debug("Nitter: все инстансы не дали HTML (last=%s)", last_err)
    return "", ""


# Функция: распарсить профиль (URL x.com или handle) и вернуть name/links/avatar
def parse_profile(url_or_handle: str) -> dict:
    if not url_or_handle:
        return {}

    s = (url_or_handle or "").strip()

    # попытка вытащить handle из x.com URL
    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
        (s + "/"),
        re.I,
    )
    if m:
        handle = m.group(1)
    else:
        # поддержка форматов: handle или @handle
        mm = re.match(r"^@?([A-Za-z0-9_]{1,15})$", s)
        handle = mm.group(1) if mm else ""

    if not handle:
        return {}

    html, inst = fetch_profile_html(handle, probe_log=True)
    if not html or not inst:
        return {}

    soup = BeautifulSoup(html, "html.parser")

    # имя профиля
    name_tag = soup.select_one(".profile-card-fullname") or soup.select_one(
        ".profile-card .profile-name-full"
    )
    name = (name_tag.get_text(strip=True) if name_tag else "") or ""

    # ссылки/аватар через probe
    avatar_raw, avatar_norm, links = _probe_profile(html, inst, handle)
    avatar_norm = _normalize_avatar(avatar_norm or "")

    return {
        "links": list(links),
        "avatar": avatar_norm,
        "name": name,
    }
