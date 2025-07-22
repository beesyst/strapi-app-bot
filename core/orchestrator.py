import asyncio
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
from core.web_parser import (
    collect_all_socials,
    get_domain_name,
)

TEMPLATE_PATH = "templates/main_template.json"
CENTRAL_CONFIG_PATH = "config/config.json"
APPS_CONFIG_DIR = "config/apps"
STORAGE_DIR = "storage/apps"

spinner_frames = ["/", "-", "\\", "|"]


# Загрузка шаблона main.json
def load_main_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# Создает папку проекта если ее нет
def create_project_folder(app_name, domain):
    storage_path = os.path.join(STORAGE_DIR, app_name, domain)
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


# Сохраняет main.json
def save_main_json(storage_path, data):
    json_path = os.path.join(storage_path, "main.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log_info(f"main.json сохранён: {json_path}")
    return json_path


# Сравнивает новый и старый main.json
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


# Крутит спиннер, пока stop_event не set
def spinner_task(text, stop_event):
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        print(f"\r[{spin}] {text} ", end="", flush=True)
        idx += 1
        time.sleep(0.13)
    print("\r" + " " * (len(text) + 10) + "\r", end="", flush=True)


# Асинхронно обертка для enrich_with_coin_id (он блокирующий)
async def enrich_coin_async(main_data, executor):
    loop = asyncio.get_event_loop()
    from core.coingecko_parser import enrich_with_coin_id

    return await loop.run_in_executor(executor, enrich_with_coin_id, main_data)


# Асинхронно генерируем и сохраняем проект
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
        storage_path = create_project_folder(app_name, domain)
        # Сбор соцсетей, docs, аватара
        main_data, avatar_path = collect_all_socials(url, main_template, storage_path)
        # CoinGecko enrichment
        main_data = await enrich_coin_async(main_data, executor)
        main_json_path = os.path.join(storage_path, "main.json")
        status = compare_main_json(main_json_path, main_data)
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


# Главный асинхронный пайплайн - проекты идут по очереди
async def orchestrate_all():
    with open(CENTRAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        central_config = json.load(f)
    main_template = load_main_template()
    prompts = load_prompts()
    openai_cfg = load_openai_config()
    executor = ThreadPoolExecutor(max_workers=8)
    for app in central_config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        print(f"[app] {app_name} start")
        app_config_path = os.path.join(APPS_CONFIG_DIR, f"{app_name}.json")
        if not os.path.exists(app_config_path):
            log_warning(f"Config for app {app_name} not found, skipping")
            continue
        with open(app_config_path, "r", encoding="utf-8") as f:
            app_config = json.load(f)
        for url in app_config["partners"]:
            domain = get_domain_name(url)
            start_time = time.time()
            status = await process_partner(
                app_name, domain, url, main_template, prompts, openai_cfg, executor
            )
            elapsed = int(time.time() - start_time)
            # Финальный вывод статуса
            if status == "add":
                print(f"[add] {app_name} - {url} - {elapsed} sec")
            elif status == "update":
                print(f"[update] {app_name} - {url} - {elapsed} sec")
            elif status == "skip":
                print(f"[skip] {app_name} - {url} - {elapsed} sec")
            else:
                print(f"[error] {app_name} - {url} - {elapsed} sec")
        print(f"[app] {app_name} done")


# Синхронизация с Strapi
def sync_projects_with_terminal_status(config_path):
    from core.api_strapi import sync_projects_with_terminal_status as strapi_sync

    strapi_sync(config_path)


# Главная точка запуска пайплайна
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
