# core/coingecko_parser.py

import time

import requests
from core.log_utils import log_info, log_warning

COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"


# Быстрый поиск coin id на Coingecko по названию или тикеру.
def search_coin_id(query, retries=3):
    for attempt in range(retries):
        try:
            log_info(f"[coingecko_parser] Поиск CoinGecko ID по имени/тикеру: {query}")
            resp = requests.get(
                f"{COINGECKO_API_BASE}/search",
                params={"query": query},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 429:
                log_warning(
                    "[coingecko_parser] search status: 429 (rate limit), жду 8 сек"
                )
                time.sleep(8)
                continue
            if resp.status_code != 200:
                log_warning(f"[coingecko_parser] search status: {resp.status_code}")
                return ""
            data = resp.json()
            coins = data.get("coins", [])
            if not coins:
                return ""
            query_l = query.lower()
            for coin in coins:
                # Прямое совпадение по имени или символу
                if coin["name"].lower() == query_l or coin["symbol"].lower() == query_l:
                    log_info(
                        f"[coingecko_parser] Найдено точное совпадение: {coin['id']}"
                    )
                    return coin["id"]
            # Фоллбек: просто первый найденный
            log_info(
                f"[coingecko_parser] Используем первый найденный: {coins[0]['id']}"
            )
            return coins[0]["id"]
        except Exception as e:
            log_warning(f"[coingecko_parser] Ошибка поиска по имени: {e}")
            time.sleep(3)
    return ""


# Медленный fallback: ищет coin id на Coingecko по домену
def search_coin_id_by_website(website_url, retries=3, max_coins=10):
    for attempt in range(retries):
        try:
            log_info(f"[coingecko_parser] Медленный поиск по сайту: {website_url}")
            domain = (
                website_url.lower()
                .replace("https://", "")
                .replace("http://", "")
                .split("/")[0]
            )
            resp = requests.get(
                f"{COINGECKO_API_BASE}/coins/list",
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 429:
                log_warning(
                    "[coingecko_parser] /coins/list status: 429 (rate limit), жду 8 сек"
                )
                time.sleep(8)
                continue
            if resp.status_code != 200:
                log_warning(
                    f"[coingecko_parser] /coins/list status: {resp.status_code}"
                )
                return ""
            coins = resp.json()
            for idx, coin in enumerate(coins):
                if idx >= max_coins:
                    log_warning(
                        f"[coingecko_parser] Достигнут лимит {max_coins} монет при fallback!"
                    )
                    break
                coin_id = coin["id"]
                try:
                    details = requests.get(
                        f"{COINGECKO_API_BASE}/coins/{coin_id}",
                        timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if details.status_code == 429:
                        log_warning(
                            f"[coingecko_parser] /coins/{coin_id} status: 429, жду 8 сек"
                        )
                        time.sleep(8)
                        continue
                    if details.status_code != 200:
                        continue
                    details_json = details.json()
                    homepage_list = details_json.get("links", {}).get("homepage", [])
                    for url in homepage_list:
                        if url and domain in url:
                            log_info(f"[coingecko_parser] Найдено по домену: {coin_id}")
                            return coin_id
                except Exception:
                    continue
                time.sleep(0.2)  # чтобы не словить бан по API
            log_info(f"[coingecko_parser] Монета по домену не найдена: {domain}")
            return ""
        except Exception as e:
            log_warning(f"[coingecko_parser] Ошибка поиска по сайту: {e}")
            time.sleep(3)
    return ""


# Комбинированный поиск - сначала по имени, потом по сайту
def get_coin_id_best(name, website_url):
    coin_id = search_coin_id(name)
    if not coin_id and website_url:
        coin_id = search_coin_id_by_website(website_url)
    return coin_id


# Тестовый пример
if __name__ == "__main__":
    print(get_coin_id_best("Eclipse", "https://www.eclipse.xyz/"))
