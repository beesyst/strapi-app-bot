import os
import subprocess
import sys

# Корень
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

VENV_PATH = os.path.join(ROOT_DIR, "venv")
INSTALL_PATH = os.path.join(ROOT_DIR, "core", "install.py")

# Если не в venv - создать и перейти
if sys.prefix == sys.base_prefix:
    if not os.path.isdir(VENV_PATH):
        print("[start] Виртуальное окружение не найдено, создаю venv ...")
        subprocess.run(["python3", "-m", "venv", VENV_PATH], check=True)
    # Запустить текущий скрипт через python из venv
    py_in_venv = os.path.join(VENV_PATH, "bin", "python")
    if not os.path.exists(py_in_venv):
        py_in_venv = os.path.join(VENV_PATH, "Scripts", "python.exe")
    os.execv(py_in_venv, [py_in_venv] + sys.argv)

# Уже внутри venv - установить зависимости через install.py
subprocess.run([sys.executable, INSTALL_PATH], check=True)

# Теперь можно делать импорты (всё уже установлено)
import copy
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.web_parser import SOCIAL_PATTERNS, extract_social_links

# Логирование
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "host.log")
logging.basicConfig(
    filename=LOG_FILE,
    filemode="w",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def log_info(msg):
    logging.info(msg)


def log_warning(msg):
    logging.warning(msg)


def log_critical(msg):
    logging.critical(msg)


def log_error(msg):
    logging.error(msg)


# Пути
CENTRAL_CONFIG_PATH = "config/config.json"
TEMPLATE_PATH = "templates/main_template.json"


# Fingerprint Suite fetch (Playwright+browser_fetch.js)
def fetch_url_html_browser(url):
    NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
    script_path = os.path.join(NODE_CORE_DIR, "browser_fetch.js")
    try:
        result = subprocess.run(
            ["node", script_path, url],
            cwd=NODE_CORE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return result.stdout
        else:
            log_warning(f"browser_fetch.js error: {result.stderr}")
            return ""
    except Exception as e:
        log_warning(f"Ошибка browser_fetch.js: {e}")
        return ""


# Requests/bs4, fallback на Fingerprint Suite если Cloudflare/блок
def fetch_url_html_pipeline(url):
    html = fetch_url_html(url)
    if (
        not html
        or "cf-browser-verification" in html
        or "enable JavaScript" in html
        or "Checking your browser" in html
        or "<title>Access denied</title>" in html
    ):
        log_info(f"Пробуем получить {url} через Fingerprint Suite (browser_fetch.js)")
        html = fetch_url_html_browser(url)
    return html


# Утилиты
def normalize_youtube(url):
    m = re.match(r"(https://www\.youtube\.com/@[\w\d\-_]+)", url)
    if m:
        return m.group(1)
    m = re.match(r"https://www\.youtube\.com/@([\w\d\-_]+)", url)
    if m:
        return f"https://www.youtube.com/@{m.group(1)}"
    if "youtube.com/@" in url:
        return "https://www.youtube.com/@" + url.split("/@")[1].split("/")[0]
    return ""


def load_main_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_domain_name(url):
    domain = urlparse(url).netloc
    return domain.replace("www.", "").split(".")[0]


def create_project_folder(domain):
    storage_path = f"storage/total/{domain}"
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


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


def normalize_socials(socials):
    if socials.get("twitterURL"):
        socials["twitterURL"] = socials["twitterURL"].replace("twitter.com", "x.com")
    return socials


def fetch_url_html(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        return requests.get(url, headers=headers, timeout=10).text
    except Exception as e:
        log_warning(f"Ошибка получения HTML {url}: {e}")
        return ""


# Твиттер/X — всегда через playwright+fingerprint-injector
def get_links_from_x_profile(profile_url):
    NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
    NODE_MODULES_PATH = os.path.join(NODE_CORE_DIR, "node_modules")
    if not os.path.isdir(NODE_MODULES_PATH):
        log_info(
            "Устанавливаю npm-зависимости для Playwright + fingerprint-injector ..."
        )
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
            log_info("npm-зависимости установлены.")
        except Exception as e:
            log_error(f"Ошибка npm install: {e}")
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
                log_error(
                    f"Ошибка парсера twitter_parser.js (JSON): {e}, RAW: {result.stdout}"
                )
                return {"links": [], "avatar": ""}
        else:
            log_error(f"Ошибка парсера twitter_parser.js: {result.stderr}")
            return {"links": [], "avatar": ""}
    except Exception as e:
        log_critical(f"Ошибка запуска twitter_parser.js: {e}")
        return {"links": [], "avatar": ""}


# Коллекционные сервисы (через пайплайн)
def fetch_link_collection(link_collection_url, socials, social_keys_patterns):
    html = fetch_url_html_pipeline(link_collection_url)
    try:
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
        log_info(f"Найдено на {link_collection_url}: {found_on_page}")
    except Exception as e:
        log_warning(f"Ошибка парсинга коллекционной ссылки {link_collection_url}: {e}")
    return socials


# Основной пайплайн
def main():
    main_template = load_main_template()
    with open(CENTRAL_CONFIG_PATH, "r") as f:
        central_config = json.load(f)
    link_collections = central_config.get("link_collections", [])
    social_keys_patterns = SOCIAL_PATTERNS

    for app in central_config["apps"]:
        app_name = app["app"]
        app_config_path = f"config/apps/{app_name}.json"
        if not os.path.exists(app_config_path):
            log_warning(f"Конфиг {app_config_path} не найден, пропуск.")
            continue

        with open(app_config_path, "r") as f:
            config = json.load(f)

        for url in config["partners"]:
            domain = get_domain_name(url)
            storage_path = create_project_folder(domain)
            main_json_path = os.path.join(storage_path, "main.json")

            if os.path.exists(main_json_path):
                log_info(
                    f"Папка {storage_path} уже есть, main.json найден. Пропускаем запись."
                )
                continue

            main_data = copy.deepcopy(main_template)
            found_socials = {}

            log_info(f"Переходим по ссылке: {url}")
            try:
                # Пайплайн: сначала requests/bs4, если fail → Fingerprint Suite
                html = fetch_url_html_pipeline(url)
                socials = extract_social_links(html, url)
                found_socials.update({k: v for k, v in socials.items() if v})
                log_info(f"Найдено соцсетей: {json.dumps(socials, ensure_ascii=False)}")

                # То же для внутренних ссылок
                internal_links = get_internal_links(html, url, max_links=10)
                log_info(f"Внутренние ссылки: {internal_links}")
                for link in internal_links:
                    try:
                        page_html = fetch_url_html_pipeline(link)
                        page_socials = extract_social_links(page_html, link)
                        for k, v in page_socials.items():
                            if v and not found_socials.get(k):
                                found_socials[k] = v
                    except Exception as e:
                        log_warning(f"Ошибка парсинга {link}: {e}")

                found_socials = normalize_socials(found_socials)

                # Docs-URL
                if found_socials.get("documentURL"):
                    docs_url = found_socials["documentURL"]
                    log_info(f"Переход на docs: {docs_url}")
                    try:
                        docs_html = fetch_url_html_pipeline(docs_url)
                        docs_socials = extract_social_links(docs_html, docs_url)
                        log_info(
                            f"Docs соцсети: {json.dumps(docs_socials, ensure_ascii=False)}"
                        )
                        for k, v in docs_socials.items():
                            if v and not found_socials.get(k):
                                found_socials[k] = v
                    except Exception as e:
                        log_warning(f"Ошибка docs: {e}")

                # Twitter/X — всегда через playwright+fingerprint-injector
                twitter_url = found_socials.get("twitterURL", "")
                if twitter_url:
                    log_info(
                        f"Переход на twitter через Playwright+fingerprint: {twitter_url}"
                    )
                    twitter_result = get_links_from_x_profile(twitter_url)
                    bio_links = twitter_result.get("links", [])
                    avatar_url = twitter_result.get("avatar", "")
                    log_info(f"Ссылки из twitter: {bio_links}")
                    if avatar_url:
                        try:
                            avatar_url_400 = re.sub(
                                r"_\d{2,4}x\d{2,4}\.jpg", "_400x400.jpg", avatar_url
                            )
                            log_info(f"Ссылка на лого: {avatar_url_400}")
                            logo_filename = f"{domain.lower()}.jpg"
                            avatar_path = os.path.join(storage_path, logo_filename)
                            avatar_data = requests.get(
                                avatar_url_400, timeout=10
                            ).content
                            with open(avatar_path, "wb") as imgf:
                                imgf.write(avatar_data)
                            log_info(f"Лого сохранен в {avatar_path}")
                            main_data["svgLogo"] = logo_filename
                        except Exception as e:
                            log_warning(f"Ошибка скачивания аватара: {e}")
                    for bio_url in bio_links:
                        for k, pattern in social_keys_patterns.items():
                            if not found_socials.get(k) or found_socials[k] == "":
                                if re.search(pattern, bio_url):
                                    found_socials[k] = bio_url
                        if any(
                            col in urlparse(bio_url).netloc for col in link_collections
                        ):
                            log_info(f"Переход на коллекционный сервис: {bio_url}")
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

                yt = final_socials.get("youtubeURL", "")
                final_socials["youtubeURL"] = normalize_youtube(yt)

                if final_socials["mediumURL"] and not final_socials[
                    "mediumURL"
                ].startswith("https://medium.com/@"):
                    final_socials["mediumURL"] = ""

                main_data["socialLinks"] = final_socials
                main_data["name"] = domain.capitalize()

                with open(main_json_path, "w", encoding="utf-8") as f:
                    json.dump(main_data, f, ensure_ascii=False, indent=2)
                log_info(f"Данные сохранены в {main_json_path}")

            except Exception as e:
                log_critical(f"Ошибка парсинга {url}: {e}")

    print("Готово!")


if __name__ == "__main__":
    main()
