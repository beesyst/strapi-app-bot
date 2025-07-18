import asyncio
import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core.api_ai import (
    ai_generate_content_markdown,
    ai_generate_short_desc,
    load_openai_config,
    load_prompts,
)
from core.log_utils import log_critical, log_info, log_warning

# Выносим get_links_from_x_profile в web_parser
from core.web_parser import (
    extract_social_links,
    fetch_url_html,
    get_domain_name,
    get_internal_links,
    get_links_from_x_profile,
    normalize_socials,
)

TEMPLATE_PATH = "templates/main_template.json"
CENTRAL_CONFIG_PATH = "config/config.json"
APPS_CONFIG_DIR = "config/apps"
STORAGE_DIR = "storage/apps"

spinner_frames = ["/", "-", "\\", "|"]


def load_main_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_project_folder(app_name, domain):
    storage_path = os.path.join(STORAGE_DIR, app_name, domain)
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


def save_main_json(storage_path, data):
    json_path = os.path.join(storage_path, "main.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log_info(f"main.json сохранён: {json_path}")
    return json_path


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


def spinner_task(text, stop_event):
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        print(f"\r[{spin}] {text} ", end="", flush=True)
        idx += 1
        time.sleep(0.13)
    print("\r" + " " * (len(text) + 10) + "\r", end="", flush=True)


# Сбор данных по проекту (только orchestration, все парсинг-функции — импортируем)
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
        # Docs
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
        # Twitter/X (Node.js parser)
        if found_socials.get("twitterURL"):
            twitter_result = get_links_from_x_profile(found_socials["twitterURL"])
            bio_links = twitter_result.get("links", [])
            avatar_url = twitter_result.get("avatar", "")
            # Качаем аватар
            if avatar_url:
                logo_filename = f"{domain.lower()}.jpg"
                avatar_path = os.path.join(storage_path, logo_filename)
                try:
                    import requests

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
    from core.api_strapi import sync_projects_with_terminal_status as strapi_sync

    strapi_sync(config_path)


def run_pipeline():
    asyncio.run(orchestrate_all())
    sync_projects_with_terminal_status(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "config.json",
        )
    )


if __name__ == "__main__":
    run_pipeline()
