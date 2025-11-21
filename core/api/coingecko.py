import json
import re
import time
from urllib.parse import urlparse

import requests
from core.log_utils import get_logger
from core.normalize import (
    brand_from_url,
    force_https,
    normalize_query,
    normalize_socials,
)
from core.paths import CONFIG_JSON  # используем единый файл путей

# Логгер
logger = get_logger("coingecko")


# Функция: загрузка базового URL API CoinGecko из config.json
def load_coingecko_api_base():
    """
    Читает config/config.json и вытаскивает поле coingecko.api_base.
    Используется один раз при импорте модуля.
    """
    with open(CONFIG_JSON, "r", encoding="utf-8") as f:
        config = json.load(f)
    coingecko_cfg = config.get("coingecko", {})
    api_base = coingecko_cfg.get("api_base")
    if not api_base:
        raise ValueError("Не задан coingecko.api_base в config/config.json")
    return api_base


COINGECKO_API_BASE = load_coingecko_api_base()


# Вспомогательная функция: безопасный запрос к CoinGecko с базовой обработкой 429/ошибок
def _request_json(
    path: str, params: dict | None = None, timeout: int = 10, retries: int = 3
):
    """
    Унифицированный запрос к CoinGecko:
    - не спамит логами (всё внутри без INFO/WARNING, кроме крайней необходимости);
    - аккуратно обрабатывает 429 (rate limit) с небольшими паузами;
    - возвращает dict/список или None при ошибке.
    """
    url = f"{COINGECKO_API_BASE}{path}"
    params = params or {}

    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 429:
                # Без лишних логов — просто подождали и повторили
                time.sleep(6)
                continue
            if resp.status_code != 200:
                # Тихо выходим без спама в лог
                return None
            return resp.json()
        except Exception:
            # Никаких подробных логов — просто короткая пауза и повтор
            time.sleep(2)

    return None


# Вспомогательная функция: вытащить handle из twitter/x URL
def _twitter_handle_from_url(url: str) -> str:
    """
    Извлекает @handle из URL вида:
    - https://twitter.com/CelestiaOrg
    - https://x.com/CelestiaOrg
    """
    if not url:
        return ""
    m = re.match(
        r"^https?://(?:www\.)?(?:twitter\.com|x\.com)/([^/?#]+)",
        url.strip(),
        re.IGNORECASE,
    )
    return (m.group(1) or "").strip().lower() if m else ""


# Вспомогательная функция: классификация URL в social-ключ
def _map_url_to_social_key(raw_url: str) -> tuple[str | None, str]:
    """
    По домену определяет тип соцсети:
    - twitter.com/x.com → twitterURL
    - t.me/telegram.me → telegramURL
    - discord.gg/discord.com → discordURL
    - github.com → githubURL
    - reddit.com → redditURL
    - youtube.com/youtu.be → youtubeURL
    Остальные ссылки здесь не маркируем как websiteURL — сайт уже есть из web-парсера.
    """
    if not raw_url:
        return None, ""
    url = force_https(raw_url.strip())
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        host = ""

    if not host:
        return None, url

    if host in ("twitter.com", "x.com"):
        return "twitterURL", url
    if host in ("t.me", "telegram.me"):
        return "telegramURL", url
    if host.startswith("discord.") or host == "discord.gg":
        return "discordURL", url
    if host == "github.com":
        return "githubURL", url
    if host.endswith("reddit.com"):
        return "redditURL", url
    if "youtube.com" in host or "youtu.be" in host:
        return "youtubeURL", url

    return None, url


# Быстрый поиск coin id на CoinGecko по текстовому запросу (имя, тикер, домен, handle)
def search_coin_id(query: str, retries: int = 3) -> str:
    """
    /search по произвольной строке (имя проекта, тикер, домен, handle и т.п.).
    Логика:
    - нормализуем запрос;
    - пытаемся найти точное совпадение по name/symbol/id;
    - иначе берём первое более-менее подходящее;
    - без лишних логов — просто возвращаем id или пустую строку.
    """
    q_api = normalize_query(query) or (query or "").strip()
    if not q_api:
        return ""

    data = _request_json(
        "/search",
        params={"query": q_api},
        timeout=10,
        retries=retries,
    )
    if not data:
        return ""

    coins = data.get("coins") or []
    if not coins:
        return ""

    query_norm = normalize_query(query)

    # 1) точное совпадение
    for coin in coins:
        name_n = normalize_query(coin.get("name", ""))
        sym_n = normalize_query(coin.get("symbol", ""))
        id_n = normalize_query(coin.get("id", ""))
        if query_norm and (
            name_n == query_norm or sym_n == query_norm or id_n == query_norm
        ):
            return coin.get("id", "")

    # 2) подстрока в name/symbol
    for coin in coins:
        name_n = normalize_query(coin.get("name", ""))
        sym_n = normalize_query(coin.get("symbol", ""))
        if query_norm and (query_norm in name_n or query_norm in sym_n):
            return coin.get("id", "")

    # 3) первый из списка
    return coins[0].get("id", "") if coins else ""


# Комбинированный поиск coin id: имя проекта, домен и twitter handle
def get_coin_id_best(name: str, website_url: str = "", twitter_url: str = "") -> str:
    """
    Собирает несколько кандидатов для поиска:
    1) имя проекта (name)
    2) бренд из домена (brand_from_url(website_url))
    3) twitter handle из URL
    И по очереди ищет id через /search.
    """
    candidates: list[str] = []

    if name:
        q = normalize_query(name) or name.strip()
        if q:
            candidates.append(q)

    if website_url:
        brand = brand_from_url(website_url)
        if brand:
            candidates.append(brand)

    if twitter_url:
        handle = _twitter_handle_from_url(twitter_url)
        if handle:
            candidates.append(handle)

    # Убираем дубли, сохраняем порядок
    seen = set()
    uniq_candidates = []
    for q in candidates:
        if q not in seen:
            seen.add(q)
            uniq_candidates.append(q)

    for q in uniq_candidates:
        coin_id = search_coin_id(q)
        if coin_id:
            return coin_id

    return ""


# Вспомогательная функция: получить соцсети токена из CoinGecko /coins/{id}
def _get_coin_socials_from_api(coin_id: str) -> tuple[dict, dict | None]:
    """
    Тянет /coins/{id} (без лишних данных) и вытаскивает соцлинки:
    - websiteURL (из homepage)
    - twitterURL (twitter_screen_name + URL'ы из ссылок)
    - telegramURL / discordURL / githubURL / redditURL / youtubeURL
    Возвращает (socials_dict, raw_json).
    """
    if not coin_id:
        return {}, None

    data = _request_json(
        f"/coins/{coin_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "true",
            "developer_data": "true",
            "sparkline": "false",
        },
        timeout=15,
        retries=2,
    )
    if not data:
        return {}, None

    links = data.get("links") or {}

    socials = {
        "websiteURL": "",
        "twitterURL": "",
        "telegramURL": "",
        "discordURL": "",
        "githubURL": "",
        "redditURL": "",
        "youtubeURL": "",
        "linkedinURL": "",
    }

    homepage_list = [u for u in (links.get("homepage") or []) if u]
    chat_urls = [u for u in (links.get("chat_url") or []) if u]
    forum_urls = [u for u in (links.get("official_forum_url") or []) if u]
    announcement_urls = [u for u in (links.get("announcement_url") or []) if u]
    subreddit_url = (links.get("subreddit_url") or "").strip()
    github_urls = (links.get("repos_url") or {}).get("github") or []

    urls: list[str] = []
    urls.extend(chat_urls)
    urls.extend(forum_urls)
    urls.extend(announcement_urls)
    urls.extend(github_urls)
    if subreddit_url:
        urls.append(subreddit_url)

    # websiteURL отдельно (homepage)
    if homepage_list:
        hp = force_https(homepage_list[0])
        if hp:
            socials["websiteURL"] = hp

    # общая раскладка ссылок
    for raw in urls:
        key, cleaned = _map_url_to_social_key(raw)
        if not key or not cleaned:
            continue
        if not socials.get(key):
            socials[key] = cleaned

    # twitter из twitter_screen_name, если ещё не заполнен
    tw_screen = (links.get("twitter_screen_name") or "").strip()
    if tw_screen and not socials.get("twitterURL"):
        socials["twitterURL"] = force_https(f"https://x.com/{tw_screen}")

    return socials, data


# Вспомогательная функция: проверка, совпадает ли токен по сайту/твиттеру с нашим проектом
def _token_links_match(project_socials: dict, cg_socials: dict) -> bool:
    """
    Обязательное условие:
    - если у проекта есть websiteURL → домен должен совпасть с homepage из CoinGecko;
    - если у проекта есть twitterURL → handle должен совпасть с twitter из CoinGecko;
    Достаточно совпадения по одному из этих каналов (website или twitter).
    """
    proj_web = (project_socials.get("websiteURL") or "").strip()
    cg_web = (cg_socials.get("websiteURL") or "").strip()

    proj_tw = (project_socials.get("twitterURL") or "").strip().lower()
    cg_tw = (cg_socials.get("twitterURL") or "").strip().lower()

    # Сайт
    if proj_web and cg_web:
        try:
            proj_brand = brand_from_url(proj_web)
            cg_brand = brand_from_url(cg_web)
            if proj_brand and cg_brand and proj_brand == cg_brand:
                return True
        except Exception:
            pass

    # Твиттер
    if proj_tw and cg_tw:
        try:
            proj_handle = _twitter_handle_from_url(proj_tw)
            cg_handle = _twitter_handle_from_url(cg_tw)
            if proj_handle and cg_handle and proj_handle == cg_handle:
                return True
        except Exception:
            pass

    return False


# Основная функция обогащения main_data CoinGecko ID + соцсети из CoinGecko
def enrich_with_coin_id(main_data: dict) -> dict:
    """
    Обогащает main_data данными из CoinGecko:
    1) Логирует единый старт:
       [INFO] - [coingecko] Поиск API ID в Coingecko - {name} - {websiteURL}
    2) Ищет coin_id по имени/домену/twitter handle.
    3) Тянет /coins/{id}, вытаскивает соцсети, проверяет совпадение по сайту/твиттеру.
       Если ни сайт, ни твиттер не совпадают — считаем, что это не наш токен.
    4) При успехе:
       - мержит соцсети из CoinGecko в main_data["socialLinks"] (как линк-агрегатор, не перетирая уже найденные);
       - пишет main_data["coinData"] = {"coin": coin_id};
       - логирует итог:
         [INFO] - [coingecko] Coingecko обогащение {websiteURL}: {socials + 'token': coin_id}
    5) При неуспехе:
       - main_data["coinData"] = {"coin": ""};
       - логирует:
         [INFO] - [coingecko] Токен в Coingecko не найден
    """
    main_data = main_data or {}
    social_links = main_data.get("socialLinks") or {}
    website_url = (social_links.get("websiteURL") or "").strip()
    twitter_url = (social_links.get("twitterURL") or "").strip()
    name = (main_data.get("name") or "").strip()

    # Имя на случай пустого name — берём бренд из URL
    if not name and website_url:
        name = brand_from_url(website_url) or ""

    logger.info(
        "Поиск API ID в Coingecko - %s - %s",
        name or "unknown",
        website_url or "N/A",
    )

    # Базовое условие: нужен хотя бы сайт или твиттер, иначе токен считаем неподходящим
    if not website_url and not twitter_url and not name:
        main_data["coinData"] = {"coin": ""}
        logger.info("Токен в Coingecko не найден")
        return main_data

    # Поиск самого id токена
    coin_id = get_coin_id_best(
        name=name, website_url=website_url, twitter_url=twitter_url
    )
    if not coin_id:
        main_data["coinData"] = {"coin": ""}
        logger.info("Токен в Coingecko не найден")
        return main_data

    # Соцсети и детали токена
    cg_socials, _raw = _get_coin_socials_from_api(coin_id)
    if not cg_socials:
        main_data["coinData"] = {"coin": ""}
        logger.info("Токен в Coingecko не найден")
        return main_data

    # Валидация токена по сайту/твиттеру
    if not _token_links_match(social_links, cg_socials):
        main_data["coinData"] = {"coin": ""}
        logger.info("Токен в Coingecko не найден")
        return main_data

    # Мерж соцсетей из CoinGecko как из линк-агрегатора (не перетираем уже найденные)
    for key, val in cg_socials.items():
        if not val:
            continue
        if key in social_links and social_links.get(key):
            continue
        social_links[key] = val

    # Нормализация и https
    social_links = normalize_socials(social_links)
    for k, v in list(social_links.items()):
        if isinstance(v, str) and v:
            social_links[k] = force_https(v)

    main_data["socialLinks"] = social_links
    main_data["coinData"] = {"coin": coin_id}

    merged_preview = {k: v for k, v in social_links.items() if v}
    merged_preview["token"] = coin_id

    logger.info(
        "Coingecko обогащение %s: %s",
        website_url or (name or "N/A"),
        merged_preview,
    )

    return main_data


# Тестовый пример запуска (локально)
if __name__ == "__main__":
    example = {
        "name": "Eclipse",
        "socialLinks": {"websiteURL": "https://www.eclipse.xyz/"},
    }
    print(enrich_with_coin_id(example))
