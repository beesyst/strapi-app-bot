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
from core.api_strapi import sync_projects_with_terminal_status as strapi_sync
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


# Создание папки для проекта
def create_project_folder(app_name, domain):
    storage_path = os.path.join(STORAGE_DIR, app_name, domain)
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


# Сохранение main.json
def save_main_json(storage_path, data):
    json_path = os.path.join(storage_path, "main.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log_info(f"main.json сохранен: {json_path}")
    return json_path


# Сравнение main.json
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


# Анимация спиннера для терминала
def spinner_task(text, stop_event):
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        print(f"\r[{spin}] {text} ", end="", flush=True)
        idx += 1
        time.sleep(0.13)
    print("\r" + " " * (len(text) + 10) + "\r", end="", flush=True)


# Асинхронное обогащение main_data CoinGecko coin_id
async def enrich_coin_async(main_data, executor):
    loop = asyncio.get_event_loop()
    from core.coingecko_parser import enrich_with_coin_id

    return await loop.run_in_executor(executor, enrich_with_coin_id, main_data)


# Асинхронный сбор и генерация по одному партнеру
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
        log_info(f"START COLLECT {app_name} - {url}")

        # Сбор соцсетей
        collect_socials_future = asyncio.get_event_loop().run_in_executor(
            executor, collect_all_socials, url, main_template, storage_path
        )
        # Промпты для ИИ
        ai_prompts_future = asyncio.get_event_loop().run_in_executor(
            executor, lambda: prompts
        )

        # Ждем сбора соцсетей
        main_data, avatar_path = await collect_socials_future
        log_info(f"COLLECTED SOCIALS for {app_name} - {url}")

        # Параллельно ищем coinId и готовим ИИ генерацию
        coin_future = enrich_coin_async(main_data, executor)
        ai_prompts = await ai_prompts_future

        main_data = await coin_future
        log_info(f"COINGECKO ENRICHED for {app_name} - {url}")

        main_json_path = os.path.join(storage_path, "main.json")
        status = compare_main_json(main_json_path, main_data)
        # Генерим AI только если add/update
        if status in ("add", "update"):
            log_info(f"AI GENERATION started for {app_name} - {url}")

            # AI генерация short и content параллельно
            ai_short_task = asyncio.create_task(
                ai_generate_short_desc(main_data, ai_prompts, openai_cfg, executor)
            )
            ai_content_task = asyncio.create_task(
                ai_generate_content_markdown(
                    main_data, app_name, domain, ai_prompts, openai_cfg, executor
                )
            )
            short_desc, content_md = await asyncio.gather(
                ai_short_task, ai_content_task
            )
            if short_desc:
                main_data["shortDescription"] = short_desc.strip()
            if content_md:
                main_data["contentMarkdown"] = content_md.strip()
            save_main_json(storage_path, main_data)
            log_info(f"AI GENERATED and SAVED for {app_name} - {url}")
        else:
            log_info(f"SKIP AI GENERATION for {app_name} - {url}")

        log_info(f"{status.upper()} {app_name} - {url}")
    except Exception as e:
        log_critical(f"Ошибка обработки {url}: {e}")
        status = "error"
    finally:
        stop_event.set()
        spinner_thread.join()
    return status


# Асинхронный запуск всех задач по проектам
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
            # На каждый проект лимит по времени (например 120 сек)
            try:
                status = await asyncio.wait_for(
                    process_partner(
                        app_name,
                        domain,
                        url,
                        main_template,
                        prompts,
                        openai_cfg,
                        executor,
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                status = "error"
                log_warning(
                    f"[TIMEOUT] Превышено время на сбор {app_name} {url}, пропуск!"
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

            # Строго один вызов Strapi на add/update
            if status in ("add", "update"):
                strapi_sync(
                    os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config",
                        "config.json",
                    )
                )
        print(f"[app] {app_name} done")


# Точка входа: запускает пайплайн
def run_pipeline():
    asyncio.run(orchestrate_all())


if __name__ == "__main__":
    run_pipeline()
