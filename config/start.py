import copy
import json
import os
import re
import subprocess
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Добавляем корень проекта в PYTHONPATH
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

from core.web_parser import SOCIAL_PATTERNS, extract_social_links

CENTRAL_CONFIG_PATH = "config/config.json"
TEMPLATE_PATH = "templates/main_template.json"
LOGS_DIR = "logs"
NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
NODE_MODULES_PATH = os.path.join(NODE_CORE_DIR, "node_modules")
os.makedirs(LOGS_DIR, exist_ok=True)

# Чистим лог при запуске
with open(os.path.join(LOGS_DIR, "host.log"), "w", encoding="utf-8") as f:
    pass


# Оставляет только https://www.youtube.com/@username
def normalize_youtube(url):
    # Находит "https://www.youtube.com/@username" в начале и отрезает всё после
    m = re.match(r"(https://www\.youtube\.com/@[\w\d\-_]+)", url)
    if m:
        return m.group(1)
    m = re.match(r"https://www\.youtube\.com/@([\w\d\-_]+)", url)
    if m:
        return f"https://www.youtube.com/@{m.group(1)}"
    if "youtube.com/@" in url:
        return "https://www.youtube.com/@" + url.split("/@")[1].split("/")[0]
    return ""


# Пишет сообщение в лог-файл
def log(message):
    with open(os.path.join(LOGS_DIR, "host.log"), "a", encoding="utf-8") as f:
        f.write(message + "\n")


# Загружает шаблон main.json
def load_main_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# Получает имя домена без www и зоны
def get_domain_name(url):
    domain = urlparse(url).netloc
    return domain.replace("www.", "").split(".")[0]


# Создает папку для хранения данных проекта
def create_project_folder(domain):
    storage_path = f"storage/total/{domain}"
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


# Находит до max_links внутренних ссылок на сайте
def get_internal_links(html, base_url, max_links=10):
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href.startswith(base_url) and href not in found:
            found.add(href)
            if len(found) >= max_links:
                break
    return list(found)


# Меняет twitter.com на x.com для twitterURL
def normalize_socials(socials):
    if socials.get("twitterURL"):
        socials["twitterURL"] = socials["twitterURL"].replace("twitter.com", "x.com")
    return socials


# Получает HTML страницы с user-agent для обхода банов
def fetch_url_html(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    return requests.get(url, headers=headers, timeout=10).text


# Запускает node-парсер X (twitter) c fingerprint
def get_links_from_x_profile(profile_url):
    if not os.path.isdir(NODE_MODULES_PATH):
        log("Устанавливаю npm-зависимости для Playwright + fingerprint-injector ...")
        try:
            subprocess.run(
                ["npm", "install", "playwright", "fingerprint-injector"],
                cwd=NODE_CORE_DIR,
                check=True,
            )
            subprocess.run(
                ["npx", "playwright", "install", "chromium"],
                cwd=NODE_CORE_DIR,
                check=True,
            )
            log("npm-зависимости установлены.")
        except Exception as e:
            log(f"Ошибка npm install: {e}")
            return {"links": [], "avatar": ""}

    try:
        result = subprocess.run(
            ["node", "twitter_parser.js", profile_url],
            cwd=NODE_CORE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except Exception as e:
                log(
                    f"Ошибка парсера twitter_parser.js (JSON): {e}, RAW: {result.stdout}"
                )
                return {"links": [], "avatar": ""}
        else:
            log(f"Ошибка парсера twitter_parser.js: {result.stderr}")
            return {"links": [], "avatar": ""}
    except Exception as e:
        log(f"Ошибка запуска twitter_parser.js: {e}")
        return {"links": [], "avatar": ""}


# Парсит страницу-коллекцию ссылок (например, linktr.ee), режет ютуб-ссылки
def fetch_link_collection(link_collection_url, socials, social_keys_patterns):
    try:
        html = fetch_url_html(link_collection_url)
        soup = BeautifulSoup(html, "html.parser")
        found_on_page = []
        youtube_candidates = []
        for a in soup.find_all("a", href=True):
            url = a["href"]
            found_on_page.append(url)
            for k, pattern in social_keys_patterns.items():
                if k == "youtubeURL":
                    if re.search(pattern, url):
                        youtube_candidates.append(url)
                else:
                    if (not socials.get(k) or socials[k] == "") and re.search(
                        pattern, url
                    ):
                        socials[k] = url
        # Обрабатываем только после прохода всех ссылок!
        yt = ""
        for candidate in youtube_candidates:
            m = re.match(r"(https://www\.youtube\.com/@[\w\d\-_]+)", candidate)
            if m:
                yt = m.group(1)
                break
        if not yt:
            for candidate in youtube_candidates:
                if "youtube.com/@" in candidate:
                    yt = (
                        "https://www.youtube.com/@"
                        + candidate.split("/@")[1].split("/")[0]
                    )
                    break
        if not yt and youtube_candidates:
            yt = youtube_candidates[0]
        socials["youtubeURL"] = yt
        log(f"Найдено на {link_collection_url}: {found_on_page}")
    except Exception as e:
        log(f"Ошибка парсинга коллекционной ссылки {link_collection_url}: {e}")
    return socials


# Основной пайплайн
def main():
    main_template = load_main_template()
    with open(CENTRAL_CONFIG_PATH, "r") as f:
        central_config = json.load(f)
    link_collections = central_config.get("link_collections", [])
    social_keys_patterns = SOCIAL_PATTERNS

    for app in central_config["apps"]:
        app_config_path = f"config/apps/{app}.json"
        if not os.path.exists(app_config_path):
            log(f"Конфиг {app_config_path} не найден, пропуск.")
            continue

        with open(app_config_path, "r") as f:
            config = json.load(f)

        for url in config["partners"]:
            domain = get_domain_name(url)
            storage_path = create_project_folder(domain)
            main_json_path = os.path.join(storage_path, "main.json")

            if os.path.exists(main_json_path):
                log(
                    f"Папка {storage_path} уже есть, main.json найден. Пропускаем запись."
                )
                continue

            main_data = copy.deepcopy(main_template)
            found_socials = {}

            log(f"Переходим по ссылке: {url}")
            try:
                html = fetch_url_html(url)
                socials = extract_social_links(html, url)
                found_socials.update({k: v for k, v in socials.items() if v})
                log(f"Найдено соцсетей: {json.dumps(socials, ensure_ascii=False)}")

                internal_links = get_internal_links(html, url, max_links=10)
                log(f"Внутренние ссылки: {internal_links}")
                for link in internal_links:
                    try:
                        page_html = fetch_url_html(link)
                        page_socials = extract_social_links(page_html, link)
                        for k, v in page_socials.items():
                            if v and not found_socials.get(k):
                                found_socials[k] = v
                    except Exception as e:
                        log(f"Ошибка парсинга {link}: {e}")

                found_socials = normalize_socials(found_socials)

                # Docs-URL
                if found_socials.get("documentURL"):
                    docs_url = found_socials["documentURL"]
                    log(f"Переход на docs: {docs_url}")
                    try:
                        docs_html = fetch_url_html(docs_url)
                        docs_socials = extract_social_links(docs_html, docs_url)
                        log(
                            f"Docs соцсети: {json.dumps(docs_socials, ensure_ascii=False)}"
                        )
                        for k, v in docs_socials.items():
                            if v and not found_socials.get(k):
                                found_socials[k] = v
                    except Exception as e:
                        log(f"Ошибка docs: {e}")

                # Парсинг X/Twitter через Playwright (fingerprint)
                twitter_url = found_socials.get("twitterURL", "")
                if twitter_url:
                    log(
                        f"Переход на twitter через Playwright+fingerprint: {twitter_url}"
                    )
                    twitter_result = get_links_from_x_profile(twitter_url)
                    bio_links = twitter_result.get("links", [])
                    avatar_url = twitter_result.get("avatar", "")
                    log(f"Ссылки из twitter: {bio_links}")
                    if avatar_url:
                        try:
                            # Подменяем любой размер на 400x400
                            avatar_url_400 = re.sub(
                                r"_\d{2,4}x\d{2,4}\.jpg", "_400x400.jpg", avatar_url
                            )
                            log(f"Ссылка на лого: {avatar_url_400}")
                            logo_filename = f"{domain.lower()}.jpg"
                            avatar_path = os.path.join(storage_path, logo_filename)
                            avatar_data = requests.get(
                                avatar_url_400, timeout=10
                            ).content
                            with open(avatar_path, "wb") as imgf:
                                imgf.write(avatar_data)
                            log(f"Лого сохранен в {avatar_path}")
                            main_data["svgLogo"] = logo_filename
                        except Exception as e:
                            log(f"Ошибка скачивания аватара: {e}")
                    for bio_url in bio_links:
                        for k, pattern in social_keys_patterns.items():
                            if not found_socials.get(k) or found_socials[k] == "":
                                if re.search(pattern, bio_url):
                                    found_socials[k] = bio_url
                        if any(
                            col in urlparse(bio_url).netloc for col in link_collections
                        ):
                            log(f"Переход на коллекционный сервис: {bio_url}")
                            found_socials = fetch_link_collection(
                                bio_url, found_socials, social_keys_patterns
                            )

                if "youtubeURL" in found_socials and found_socials["youtubeURL"]:
                    found_socials["youtubeURL"] = normalize_youtube(
                        found_socials["youtubeURL"]
                    )

                # Формируем итоговые соцсети
                social_keys = list(main_template["socialLinks"].keys())
                final_socials = {k: found_socials.get(k, "") for k in social_keys}

                # Youtube: только https://www.youtube.com/@username (обрабатываем только через функцию)
                yt = final_socials.get("youtubeURL", "")
                final_socials["youtubeURL"] = normalize_youtube(yt)

                # Medium: только https://medium.com/@...
                if final_socials["mediumURL"] and not final_socials[
                    "mediumURL"
                ].startswith("https://medium.com/@"):
                    final_socials["mediumURL"] = ""

                main_data["socialLinks"] = final_socials
                main_data["name"] = domain.capitalize()

                with open(main_json_path, "w", encoding="utf-8") as f:
                    json.dump(main_data, f, ensure_ascii=False, indent=2)
                log(f"Данные сохранены в {main_json_path}")

            except Exception as e:
                log(f"Ошибка парсинга {url}: {e}")

    print("Готово!")


if __name__ == "__main__":
    main()
