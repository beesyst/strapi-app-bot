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
from core.api_strapi import try_upload_logo
from core.coingecko_parser import enrich_with_coin_id
from core.log_utils import get_logger
from core.seo_utils import build_seo_section
from core.status import (
    ADD,
    ERROR,
    SKIP,
    UPDATE,
    check_mainjson_status,
    log_mainjson_status,
)
from core.web_parser import (
    collect_social_links_main,
    fetch_twitter_avatar_and_name,
    get_domain_name,
)

# Получаем логгер orchestrator (имя модуля)
logger = get_logger("orchestrator")
strapi_logger = get_logger("strapi")

TEMPLATE_PATH = "templates/main_template.json"
CENTRAL_CONFIG_PATH = "config/config.json"
APPS_CONFIG_DIR = "config/apps"
STORAGE_DIR = "storage/apps"

spinner_frames = ["/", "-", "\\", "|"]


# Загружает основной шаблон main.json
def load_main_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# Создает папку для хранения результата по проекту
def create_project_folder(app_name, domain):
    storage_path = os.path.join(STORAGE_DIR, app_name, domain)
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


# Сохраняет main.json для проекта
def save_main_json(storage_path, data):
    json_path = os.path.join(storage_path, "main.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"main.json сохранен: {json_path}")
    return json_path


# Анимация спиннера для отображения статуса в терминале
def spinner_task(text, stop_event):
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        print(f"\r[{spin}] {text} ", end="", flush=True)
        idx += 1
        time.sleep(0.13)


# Асинхронная обработка одного партнера
async def process_partner(
    app_name,
    domain,
    url,
    main_template,
    prompts,
    openai_cfg,
    executor,
    show_status=False,
):
    start_time = time.time()
    spinner_text = f"{app_name} - {url}"
    stop_event = threading.Event()
    spinner_thread = threading.Thread(
        target=spinner_task, args=(spinner_text, stop_event)
    )
    spinner_thread.start()
    status = ERROR
    try:
        storage_path = create_project_folder(app_name, domain)
        logger.info(f"Создание main.json - {app_name} - {url}")
        logger.info(f"Сбор соц линков - {app_name} - {url}")
        logger.info(f"ИИ-генерация - {app_name} - {url}")
        logger.info(f"Поиск API ID в Coingecko - {app_name} - {url}")

        loop = asyncio.get_event_loop()
        socials_future = loop.run_in_executor(
            executor, collect_social_links_main, url, main_template, storage_path
        )
        main_data_for_ai = dict(main_template)
        main_data_for_ai["name"] = domain.capitalize()
        main_data_for_ai["socialLinks"]["websiteURL"] = url

        ai_short_future = asyncio.create_task(
            ai_generate_short_desc(main_data_for_ai, prompts, openai_cfg, executor)
        )
        ai_content_future = asyncio.create_task(
            ai_generate_content_markdown(
                main_data_for_ai, app_name, domain, prompts, openai_cfg, executor
            )
        )
        coin_future = asyncio.create_task(enrich_coin_async(main_data_for_ai, executor))

        found_socials, clean_name = await socials_future

        if found_socials.get("twitterURL"):
            twitter_future = loop.run_in_executor(
                executor,
                fetch_twitter_avatar_and_name,
                found_socials["twitterURL"],
                storage_path,
                clean_name,
            )
        else:
            twitter_future = None

        coin_result, short_desc, content_md = await asyncio.gather(
            coin_future, ai_short_future, ai_content_future
        )

        if twitter_future:
            logo_filename, real_name = await twitter_future
        else:
            logo_filename, real_name = None, None

        social_keys = list(main_template["socialLinks"].keys())
        final_socials = {k: found_socials.get(k, "") for k in social_keys}
        main_data = dict(main_template)
        main_data["socialLinks"] = final_socials
        main_data["name"] = real_name
        logger.info(f"Итоговое имя проекта: '{main_data['name']}'")
        main_data["svgLogo"] = logo_filename
        if coin_result and "coinData" in coin_result:
            main_data["coinData"] = coin_result["coinData"]
        if short_desc:
            main_data["shortDescription"] = short_desc.strip()
        if content_md:
            main_data["contentMarkdown"] = content_md.strip()
        main_data["seo"] = await build_seo_section(
            main_data, prompts, openai_cfg, executor
        )

        main_json_path = os.path.join(storage_path, "main.json")

        # --- Важно! ---
        if not os.path.exists(main_json_path):
            status = ADD
        else:
            try:
                with open(main_json_path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                status = check_mainjson_status(old_data, main_data)
            except Exception as e:
                logger.warning(f"Ошибка сравнения main.json: {e}")
                status = ADD

        if status == ADD or status == UPDATE:
            save_main_json(storage_path, main_data)
            log_mainjson_status(status, app_name, domain, url)
            logger.info(f"Готово - {app_name} - {url}")
        elif status == SKIP:
            log_mainjson_status(status, app_name, domain, url)
            logger.info(f"[skip] - {app_name} - {url}")
        else:
            log_mainjson_status(
                ERROR, app_name, domain, url, error_msg="Неизвестный статус"
            )

    except Exception as e:
        logger.critical(f"Ошибка обработки {url}: {e}")
        status = ERROR
        log_mainjson_status(ERROR, app_name, domain, url, error_msg=str(e))
    finally:
        stop_event.set()
        spinner_thread.join()
        time.sleep(0.01)
    return status


# Асинхронное обогащение данных по коину через CoinGecko
async def enrich_coin_async(main_data, executor):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, enrich_with_coin_id, main_data)


# Главная оркестрация всего пайплайна
async def orchestrate_all():
    with open(CENTRAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        central_config = json.load(f)
    strapi_sync = central_config.get("strapi_sync", True)
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
            logger.warning(f"Config for app {app_name} not found, skipping")
            continue
        with open(app_config_path, "r", encoding="utf-8") as f:
            app_config = json.load(f)
        for url in app_config["partners"]:
            domain = get_domain_name(url)
            storage_path = os.path.join(STORAGE_DIR, app_name, domain)
            main_json_path = os.path.join(storage_path, "main.json")
            start_time = time.time()
            status_main = ERROR
            try:
                status_main = await asyncio.wait_for(
                    process_partner(
                        app_name,
                        domain,
                        url,
                        main_template,
                        prompts,
                        openai_cfg,
                        executor,
                        show_status=False,
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[timeout] Превышено время на сбор {app_name} {url}, пропуск!"
                )
                print(f"[error] {app_name} - {url} - timeout!")
                continue

            elapsed = int(time.time() - start_time)
            # strapi_sync: false - печать только main.json статуса
            if not strapi_sync:
                if status_main == ERROR:
                    extra = " [main.json Error]"
                else:
                    extra = ""
                print(
                    f"\r[{status_main}] {app_name} - {url} - {elapsed} sec{extra}",
                    end="",
                    flush=True,
                )
                print()
                continue
            # strapi_sync: true - пушим в Strapi и печатаем статус
            if not os.path.exists(main_json_path):
                print(f"[error] {app_name} - {url} - {elapsed} sec [No main.json]")
                continue
            with open(main_json_path, "r", encoding="utf-8") as fjson:
                main_data = json.load(fjson)
            api_url = app.get("api_url", "")
            api_token = app.get("api_token", "")
            if api_url and api_token:
                from core.api_strapi import (
                    ERROR as STRAPI_ERROR,
                )
                from core.api_strapi import (
                    SKIP as STRAPI_SKIP,
                )
                from core.api_strapi import (
                    create_project,
                )

                status_strapi, project_id = create_project(
                    api_url,
                    api_token,
                    main_data,
                    app_name=app_name,
                    domain=domain,
                    url=url,
                )
                final_status = (
                    status_strapi if status_strapi != STRAPI_ERROR else "error"
                )
                extra = ""
                if status_strapi == STRAPI_ERROR:
                    extra = " [Strapi Create Failed]"
                elif status_strapi == STRAPI_SKIP:
                    extra = " [Already exists]"
                print(
                    f"\r[{final_status}] {app_name} - {url} - {elapsed} sec{extra}",
                    end="",
                    flush=True,
                )
                print()
                if project_id:
                    try_upload_logo(
                        main_data, storage_path, api_url, api_token, project_id
                    )
            else:
                print(f"[error] {app_name} - {url} - {elapsed} sec [No API]")
        print(f"[app] {app_name} done")


# Запуск пайплайна
def run_pipeline():
    asyncio.run(orchestrate_all())


if __name__ == "__main__":
    run_pipeline()
