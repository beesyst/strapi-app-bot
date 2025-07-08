import importlib.util
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

# Корень
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
SETUP_LOG = os.path.join(LOGS_DIR, "setup.log")
LOG_FILE = os.path.join(LOGS_DIR, "host.log")

with open(SETUP_LOG, "w", encoding="utf-8") as f:
    f.write(
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] setup.log started\n\n"
    )

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

# Уже внутри venv - установить зависимости через install.py (вывод в setup.log)
with open(SETUP_LOG, "a") as logf:
    subprocess.run([sys.executable, INSTALL_PATH], check=True, stdout=logf, stderr=logf)

# Импорты после установки зависимостей
import copy
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.web_parser import SOCIAL_PATTERNS, extract_social_links

# Логирование в host.log
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


# Пути к конфигам и шаблонам
CENTRAL_CONFIG_PATH = "config/config.json"
TEMPLATE_PATH = "templates/main_template.json"

# Спиннер
spinner_frames = ["/", "-", "\\", "|"]
spinner_running = False


def spinner_task(text, stop_event):
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        sys.stdout.write(f"\r[{spin}] {text} ")
        sys.stdout.flush()
        idx += 1
        time.sleep(0.13)
    sys.stdout.write("\r" + " " * (len(text) + 10) + "\r")


def print_ok(text):
    sys.stdout.write(f"\r[ok] {text}      \n")
    sys.stdout.flush()


def print_no(text):
    sys.stdout.write(f"\r[no] {text}      \n")
    sys.stdout.flush()


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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["npx", "playwright", "install", "chromium"],
                cwd=NODE_CORE_DIR,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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


# Основной пайплайн со спиннером
def main(app_name):
    main_template = load_main_template()
    app_config_path = f"config/apps/{app_name}.json"
    if not os.path.exists(app_config_path):
        log_warning(f"Конфиг {app_config_path} не найден, пропуск.")
        print(f"Проект {app_name}")
        return

    with open(app_config_path, "r") as f:
        config = json.load(f)

    for url in config["partners"]:
        domain = get_domain_name(url)
        storage_path = create_project_folder(domain)
        main_json_path = os.path.join(storage_path, "main.json")

        if os.path.exists(main_json_path):
            print_ok(f"{app_name} - {url}")
            log_info(
                f"Папка {storage_path} уже есть, main.json найден. Пропускаем запись."
            )
            continue

        main_data = copy.deepcopy(main_template)
        found_socials = {}

        # Запуск спиннера в отдельном потоке
        stop_event = threading.Event()
        spinner_text = f"{app_name} - {url}"
        spinner_thread = threading.Thread(
            target=spinner_task, args=(spinner_text, stop_event)
        )
        spinner_thread.start()
        status = None

        try:
            log_info(f"Переходим по ссылке: {url}")
            html = fetch_url_html_pipeline(url)
            socials = extract_social_links(html, url)
            found_socials.update({k: v for k, v in socials.items() if v})
            log_info(f"Найдено соцсетей: {json.dumps(socials, ensure_ascii=False)}")

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
                        avatar_data = requests.get(avatar_url_400, timeout=10).content
                        with open(avatar_path, "wb") as imgf:
                            imgf.write(avatar_data)
                        log_info(f"Лого сохранен в {avatar_path}")
                        main_data["svgLogo"] = logo_filename
                    except Exception as e:
                        log_warning(f"Ошибка скачивания аватара: {e}")
                for bio_url in bio_links:
                    for k, pattern in SOCIAL_PATTERNS.items():
                        if not found_socials.get(k) or found_socials[k] == "":
                            if re.search(pattern, bio_url):
                                found_socials[k] = bio_url
            if "youtubeURL" in found_socials and found_socials["youtubeURL"]:
                found_socials["youtubeURL"] = normalize_youtube(
                    found_socials["youtubeURL"]
                )

            # Формируем итоговые соцсети
            social_keys = list(main_template["socialLinks"].keys())
            final_socials = {k: found_socials.get(k, "") for k in social_keys}
            yt = final_socials.get("youtubeURL", "")
            final_socials["youtubeURL"] = normalize_youtube(yt)
            if final_socials["mediumURL"] and not final_socials["mediumURL"].startswith(
                "https://medium.com/@"
            ):
                final_socials["mediumURL"] = ""

            main_data["socialLinks"] = final_socials
            main_data["name"] = domain.capitalize()

            with open(main_json_path, "w", encoding="utf-8") as f:
                json.dump(main_data, f, ensure_ascii=False, indent=2)
            log_info(f"Данные сохранены в {main_json_path}")
            status = "ok"
        except Exception as e:
            log_critical(f"Ошибка парсинга {url}: {e}")
            status = "no"

        # Остановить спиннер и вывести статус
        stop_event.set()
        spinner_thread.join()
        if status == "ok":
            print_ok(f"{app_name} - {url}")
        else:
            print_no(f"{app_name} - {url}")


if __name__ == "__main__":
    with open(CENTRAL_CONFIG_PATH, "r") as f:
        central_config = json.load(f)
    # Сначала собираем данные по каждому app
    for app in central_config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        print(f"Проект {app_name} - сбор данных")
        main(app_name)

    # Затем AI-обработка
    try:
        api_ai_path = os.path.join(ROOT_DIR, "core", "api_ai.py")
        spec_ai = importlib.util.spec_from_file_location("api_ai", api_ai_path)
        api_ai = importlib.util.module_from_spec(spec_ai)
        spec_ai.loader.exec_module(api_ai)
        api_ai.process_all_projects()
    except Exception as e:
        print(f"[ERROR] Ошибка запуска AI генерации: {e}")

    # Только после этого отправляем всё в Strapi
    for app in central_config["apps"]:
        if not app.get("enabled", True):
            continue
        print(f"Проект {app['app']} - отправка в Strapi")
        try:
            api_strapi_path = os.path.join(ROOT_DIR, "core", "api_strapi.py")
            spec = importlib.util.spec_from_file_location("api_strapi", api_strapi_path)
            api_strapi = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(api_strapi)
            api_strapi.sync_projects(os.path.join(ROOT_DIR, "config", "config.json"))
        except Exception as e:
            print(f"[ERROR] Ошибка запуска sync_projects: {e}")
