import asyncio
import copy
import importlib.util
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from core.api_ai import (
    call_openai_api,
    load_openai_config,
    load_prompts,
    render_prompt,
)
from core.log_utils import log_critical, log_info, log_warning
from core.web_parser import extract_social_links

TEMPLATE_PATH = "templates/main_template.json"
CENTRAL_CONFIG_PATH = "config/config.json"
APPS_CONFIG_DIR = "config/apps"
STORAGE_DIR = "storage/apps"

spinner_frames = ["/", "-", "\\", "|"]


# Вызов парсера X/Twitter через Node.js
def get_links_from_x_profile(profile_url):
    import subprocess

    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    NODE_CORE_DIR = os.path.join(ROOT_DIR, "core")
    script_path = os.path.join(NODE_CORE_DIR, "twitter_parser.js")
    try:
        result = subprocess.run(
            ["node", script_path, profile_url],
            cwd=NODE_CORE_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            log_warning(f"twitter_parser.js error: {result.stderr}")
            return {"links": [], "avatar": ""}
    except Exception as e:
        log_warning(f"Ошибка запуска twitter_parser.js: {e}")
        return {"links": [], "avatar": ""}


# Загрузка шаблона
def load_main_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# Вытаскиваем домен из URL
def get_domain_name(url):
    from urllib.parse import urlparse

    domain = urlparse(url).netloc
    return domain.replace("www.", "").split(".")[0]


# Создаем папку для проекта
def create_project_folder(app_name, domain):
    storage_path = os.path.join(STORAGE_DIR, app_name, domain)
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


# Получаем HTML страницы
def fetch_url_html(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        return requests.get(url, headers=headers, timeout=10).text
    except Exception as e:
        log_warning(f"Ошибка получения HTML {url}: {e}")
        return ""


# Ищем внутренние ссылки
def get_internal_links(html, base_url, max_links=10):
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href.startswith(base_url) and href not in found:
            found.add(href)
            if len(found) >= max_links:
                break
    return list(found)


# Нормализация соц. ссылок
def normalize_socials(socials):
    if socials.get("twitterURL"):
        socials["twitterURL"] = socials["twitterURL"].replace("twitter.com", "x.com")
    return socials


# Сохраняем main.json
def save_main_json(storage_path, data):
    json_path = os.path.join(storage_path, "main.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log_info(f"main.json сохранён: {json_path}")
    return json_path


# Сравниваем содержимое main.json (чтобы понять статус)
def compare_main_json(json_path, new_data):
    if not os.path.exists(json_path):
        return "add"
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
        if old_data != new_data:
            return "update"
        else:
            return "skip"
    except Exception as e:
        log_warning(f"Ошибка сравнения main.json: {e}")
        return "add"


# Спиннер
def spinner_task(text, stop_event):
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        print(f"\r[{spin}] {text} ", end="", flush=True)
        idx += 1
        time.sleep(0.13)
    print("\r" + " " * (len(text) + 10) + "\r", end="", flush=True)


# Сбор данных по проекту
async def collect_project_data(app_name, domain, url, main_template, executor):
    def sync_collect():
        storage_path = create_project_folder(app_name, domain)
        main_data = copy.deepcopy(main_template)
        found_socials = {}
        log_info(f"Переходим по ссылке: {url}")
        html = fetch_url_html(url)
        socials = extract_social_links(html, url)
        found_socials.update({k: v for k, v in socials.items() if v})
        log_info(f"Найдено соцсетей: {json.dumps(socials, ensure_ascii=False)}")
        # Смотрим внутренние ссылки
        internal_links = get_internal_links(html, url, max_links=10)
        log_info(f"Внутренние ссылки: {internal_links}")
        for link in internal_links:
            try:
                page_html = fetch_url_html(link)
                page_socials = extract_social_links(page_html, link)
                for k, v in page_socials.items():
                    if v and not found_socials.get(k):
                        found_socials[k] = v
            except Exception as e:
                log_warning(f"Ошибка парсинга {link}: {e}")
        found_socials = normalize_socials(found_socials)
        # Док
        if found_socials.get("documentURL"):
            docs_url = found_socials["documentURL"]
            log_info(f"Переход на docs: {docs_url}")
            try:
                docs_html = fetch_url_html(docs_url)
                docs_socials = extract_social_links(docs_html, docs_url)
                log_info(
                    f"Docs соцсети: {json.dumps(docs_socials, ensure_ascii=False)}"
                )
                for k, v in docs_socials.items():
                    if v and not found_socials.get(k):
                        found_socials[k] = v
            except Exception as e:
                log_warning(f"Ошибка docs: {e}")
        # Twitter/X
        if found_socials.get("twitterURL"):
            twitter_result = get_links_from_x_profile(found_socials["twitterURL"])
            bio_links = twitter_result.get("links", [])
            avatar_url = twitter_result.get("avatar", "")
            # Качаем аватар
            if avatar_url:
                logo_filename = f"{domain.lower()}.jpg"
                avatar_path = os.path.join(storage_path, logo_filename)
                try:
                    avatar_data = requests.get(avatar_url, timeout=10).content
                    with open(avatar_path, "wb") as imgf:
                        imgf.write(avatar_data)
                    main_data["svgLogo"] = logo_filename
                except Exception as e:
                    log_warning(f"Ошибка скачивания аватара: {e}")
            # bio-ссылки
            for bio_url in bio_links:
                for k in main_template["socialLinks"].keys():
                    if k.endswith("URL") and not found_socials.get(k):
                        if bio_url.lower().startswith(k.replace("URL", "").lower()):
                            found_socials[k] = bio_url
        # Собираем итоговые соц.сети
        social_keys = list(main_template["socialLinks"].keys())
        final_socials = {k: found_socials.get(k, "") for k in social_keys}
        main_data["socialLinks"] = final_socials
        main_data["name"] = domain.capitalize()
        return main_data, storage_path

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_collect)


# AI: короткое описание
async def ai_generate_short_desc(data, prompts, openai_cfg, executor):
    def sync_ai_short():
        short_ctx = {
            "name2": data.get("name", ""),
            "website2": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        short_prompt = render_prompt(prompts["short_description"], short_ctx)
        return call_openai_api(
            short_prompt,
            openai_cfg["api_key"],
            openai_cfg["api_url"],
            openai_cfg["model"],
        )

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_short)


# AI: markdown-контент
async def ai_generate_content_markdown(
    data, app_name, domain, prompts, openai_cfg, executor
):
    def sync_ai_content():
        context1 = {
            "name": data.get("name", domain),
            "website": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        prompt1 = render_prompt(prompts["review_full"], context1)
        content1 = call_openai_api(
            prompt1,
            openai_cfg["api_key"],
            openai_cfg["api_url"],
            openai_cfg["model"],
        )
        # Проверяем связь проектов
        main_app_config_path = os.path.join("config", "apps", f"{app_name}.json")
        if os.path.exists(main_app_config_path):
            with open(main_app_config_path, "r", encoding="utf-8") as f:
                main_app_cfg = json.load(f)
            main_name = main_app_cfg.get("name", app_name.capitalize())
            main_url = main_app_cfg.get("url", "")
        else:
            main_name = app_name.capitalize()
            main_url = ""
        content2 = ""
        if domain.lower() != main_name.lower():
            context2 = {
                "name1": main_name,
                "website1": main_url,
                "name2": context1["name"],
                "website2": context1["website"],
            }
            prompt2 = render_prompt(prompts["connection"], context2)
            content2 = call_openai_api(
                prompt2,
                openai_cfg["api_key"],
                openai_cfg["api_url"],
                openai_cfg["model"],
            )
        all_content = content1
        if content2:
            all_content = (
                f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
            )
        context3 = {"connection_with": main_name if content2 else ""}
        prompt3 = render_prompt(prompts["finalize"], context3)
        final_content = call_openai_api(
            f"{all_content}\n\n{prompt3}",
            openai_cfg["api_key"],
            openai_cfg["api_url"],
            openai_cfg["model"],
        )
        return final_content

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_content)


# Парсим и AI для одного партнера
async def process_partner(
    app_name, domain, url, main_template, prompts, openai_cfg, executor
):
    spinner_text = f"{app_name} - {url}"
    stop_event = threading.Event()
    spinner_thread = threading.Thread(
        target=spinner_task, args=(spinner_text, stop_event)
    )
    spinner_thread.start()
    status = "error"
    try:
        main_data, storage_path = await collect_project_data(
            app_name, domain, url, main_template, executor
        )
        main_json_path = os.path.join(storage_path, "main.json")
        status = compare_main_json(main_json_path, main_data)
        if status in ("add", "update"):
            save_main_json(storage_path, main_data)
        # AI генерация
        ai_short = asyncio.create_task(
            ai_generate_short_desc(main_data, prompts, openai_cfg, executor)
        )
        ai_content = asyncio.create_task(
            ai_generate_content_markdown(
                main_data, app_name, domain, prompts, openai_cfg, executor
            )
        )
        short_desc = await ai_short
        content_md = await ai_content
        # Сохраняем итоговый main.json
        if short_desc:
            main_data["shortDescription"] = short_desc.strip()
        if content_md:
            main_data["contentMarkdown"] = content_md.strip()
        if status in ("add", "update"):
            save_main_json(storage_path, main_data)
        log_info(f"{status.upper()} {app_name} - {url}")
    except Exception as e:
        log_critical(f"Ошибка обработки {url}: {e}")
        status = "error"
    finally:
        stop_event.set()
        spinner_thread.join()
    return status


# Главный асинхронный пайплайн по всем проектам
async def orchestrate_all():
    with open(CENTRAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        central_config = json.load(f)
    main_template = load_main_template()
    prompts = load_prompts()
    openai_cfg = load_openai_config()
    executor = ThreadPoolExecutor(max_workers=8)
    tasks = []
    for app in central_config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        app_config_path = os.path.join(APPS_CONFIG_DIR, f"{app_name}.json")
        if not os.path.exists(app_config_path):
            log_warning(f"Config for app {app_name} not found, skipping")
            continue
        with open(app_config_path, "r", encoding="utf-8") as f:
            app_config = json.load(f)
        for url in app_config["partners"]:
            domain = get_domain_name(url)
            tasks.append(
                process_partner(
                    app_name, domain, url, main_template, prompts, openai_cfg, executor
                )
            )
    await asyncio.gather(*tasks)


# Синхронизация с Strapi и терминальный вывод статусов
def sync_projects_with_terminal_status(config_path):
    # Импортируем api_strapi динамически
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    api_strapi_path = os.path.join(ROOT_DIR, "core", "api_strapi.py")
    spec = importlib.util.spec_from_file_location("api_strapi", api_strapi_path)
    api_strapi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_strapi)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        print(f"[app] {app_name} start")
        api_url = app.get("api_url", "")
        api_token = app.get("api_token", "")
        if not api_url or not api_token:
            print(f"[error] {app_name}: нет api_url или api_token")
            continue
        partners_path = os.path.join(ROOT_DIR, "config", "apps", f"{app_name}.json")
        if not os.path.exists(partners_path):
            print(f"[error] {app_name}: нет partners config")
            continue
        with open(partners_path, "r", encoding="utf-8") as f2:
            partners_data = json.load(f2)
        for partner in partners_data.get("partners", []):
            if not partner or not partner.strip():
                continue
            domain = (
                partner.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
            )
            json_path = os.path.join(
                ROOT_DIR, "storage", "apps", app_name, domain, "main.json"
            )
            image_path = os.path.join(
                ROOT_DIR, "storage", "apps", app_name, domain, f"{domain}.jpg"
            )
            if not os.path.exists(json_path):
                print(f"[error] {app_name} - {partner}")
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f3:
                    data = json.load(f3)
                project_id = api_strapi.create_project(api_url, api_token, data)
                if project_id:
                    # Пробуем загрузить лого если оно есть
                    logo_uploaded = True
                    if data.get("svgLogo") and os.path.exists(image_path):
                        logo_result = api_strapi.upload_logo(
                            api_url, api_token, project_id, image_path
                        )
                        logo_uploaded = logo_result is not None
                    if logo_uploaded:
                        print(f"[add] {app_name} - {partner}")
                    else:
                        print(f"[error] {app_name} - {partner} (logo upload failed)")
                else:
                    print(f"[error] {app_name} - {partner}")
            except Exception as e:
                print(f"[error] {app_name} - {partner}: {e}")
        print(f"[app] {app_name} done")


# Главная точка входа
def run_pipeline():
    # Сначала сбор main.json + AI
    asyncio.run(orchestrate_all())
    # Потом синхронизируем с Strapi и показываем статусы
    sync_projects_with_terminal_status(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "config.json",
        )
    )


if __name__ == "__main__":
    run_pipeline()
