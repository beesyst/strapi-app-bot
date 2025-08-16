import json
import os
import time

import requests
from core.log_utils import get_logger
from core.normalize import brand_from_url, normalize_query

# Логгер
logger = get_logger("coingecko")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.json"
)


def load_coingecko_api_base():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("coingecko", {}).get(
        "api_base", "https://api.coingecko.com/api/v3"
    )


COINGECKO_API_BASE = load_coingecko_api_base()


# Быстрый поиск coin id на coingecko по названию или тикеру
def search_coin_id(query, retries=3):
    for attempt in range(retries):
        try:
            q_api = normalize_query(query) or (query or "").strip()
            resp = requests.get(
                f"{COINGECKO_API_BASE}/search",
                params={"query": q_api},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 429:
                logger.warning("search status: 429 (rate limit), жду 8 сек")
                time.sleep(8)
                continue
            if resp.status_code != 200:
                logger.warning(f"search status: {resp.status_code}")
                return ""
            data = resp.json()
            coins = data.get("coins", [])
            if not coins:
                return ""
            query_norm = normalize_query(query)
            for coin in coins:
                if (
                    normalize_query(coin.get("name", "")) == query_norm
                    or normalize_query(coin.get("symbol", "")) == query_norm
                    or normalize_query(coin.get("id", "")) == query_norm
                ):
                    return coin["id"]
            # Фоллбек: первый релевантный (по подстроке), затем первый вообще
            for coin in coins:
                name_n = normalize_query(coin.get("name", ""))
                sym_n = normalize_query(coin.get("symbol", ""))
                if query_norm and (query_norm in name_n or query_norm in sym_n):
                    logger.info(f"Ближайшее совпадение: {coin['id']}")
                    return coin["id"]
            logger.info(f"Используем первый найденный: {coins[0]['id']}")
            return coins[0]["id"]

        except Exception as e:
            logger.warning(f"Ошибка поиска по имени: {e}")
            time.sleep(3)
    return ""


# Медленный fallback: поиск coin id на Coingecko по домену
def search_coin_id_by_website(website_url, retries=3, max_coins=10):
    for attempt in range(retries):
        try:
            logger.info(f"Медленный поиск по сайту: {website_url}")
            domain = brand_from_url(website_url)
            resp = requests.get(
                f"{COINGECKO_API_BASE}/coins/list",
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 429:
                logger.warning("/coins/list status: 429 (rate limit), жду 8 сек")
                time.sleep(8)
                continue
            if resp.status_code != 200:
                logger.warning(f"/coins/list status: {resp.status_code}")
                return ""
            coins = resp.json()
            for idx, coin in enumerate(coins):
                if idx >= max_coins:
                    logger.warning(f"Достигнут лимит {max_coins} монет при fallback!")
                    break
                coin_id = coin["id"]
                try:
                    details = requests.get(
                        f"{COINGECKO_API_BASE}/coins/{coin_id}",
                        timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if details.status_code == 429:
                        logger.warning(f"/coins/{coin_id} status: 429, жду 8 сек")
                        time.sleep(8)
                        continue
                    if details.status_code != 200:
                        continue
                    details_json = details.json()
                    homepage_list = details_json.get("links", {}).get("homepage", [])
                    for url in homepage_list:
                        if url and brand_from_url(url) == domain:
                            logger.info(f"Найдено по домену: {coin_id}")
                            return coin_id
                except Exception:
                    continue
                time.sleep(0.2)
            logger.info(f"Монета по домену не найдена: {domain}")
            return ""
        except Exception as e:
            logger.warning(f"Ошибка поиска по сайту: {e}")
            time.sleep(3)
    return ""


# Комбинированный поиск - сначала по имени, потом по сайту
def get_coin_id_best(name, website_url):
    # по нормализованному имени (altlayer)
    coin_id = search_coin_id(normalize_query(name))
    # быстрый поиск по бренду из URL (altlayer)
    if not coin_id and website_url:
        brand = brand_from_url(website_url)
        if brand:
            coin_id = search_coin_id(brand)
    # как есть (на случай бренднеймов с пробелами и пр.)
    if not coin_id and name:
        coin_id = search_coin_id(name)
    # медленный обход /coins/{id} с homepage
    if not coin_id and website_url:
        coin_id = search_coin_id_by_website(website_url)
    return coin_id


# Обогащение main_data CoinGecko ID, результат в coinData
def enrich_with_coin_id(main_data):
    name = main_data.get("name", "")
    website_url = ""
    if "socialLinks" in main_data:
        website_url = main_data["socialLinks"].get("websiteURL", "")
    coin_id = get_coin_id_best(name, website_url)
    if coin_id:
        main_data["coinData"] = {"coin": coin_id}
        logger.info(f"CoinGecko ID найден для {name}: {coin_id}")
    else:
        main_data["coinData"] = {"coin": ""}
        logger.info(f"CoinGecko ID не найден для {name}")
    return main_data


# Тестовый пример запуска поиска
if __name__ == "__main__":
    print(get_coin_id_best("Eclipse", "https://www.eclipse.xyz/"))
