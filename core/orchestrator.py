import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core.api.ai import (
    ai_generate_content_markdown,
    ai_generate_project_categories,
    ai_generate_short_desc_with_retries,
    load_ai_config,
    load_prompts,
)
from core.api.coingecko import enrich_with_coin_id
from core.api.strapi import (
    get_project_category_ids,
    try_upload_logo,
)
from core.collector import collect_main_data
from core.log_utils import get_logger
from core.normalize import brand_from_url
from core.paths import (
    CONFIG_DIR,
    CONFIG_JSON,
    STORAGE_APPS_DIR,
    # см. примечание ниже: нужен MAIN_TEMPLATE в core/paths.py
    # если добавлен, импортируем:
    # MAIN_TEMPLATE,
)
from core.seo_utils import build_seo_section
from core.status import (
    ADD,
    ERROR,
    SKIP,
    UPDATE,
    check_mainjson_status,
    log_mainjson_status,
)

# Логгеры
logger = get_logger("orchestrator")
strapi_logger = get_logger("strapi")

# Пути (используем константы из core/paths.py)
# MAIN_TEMPLATE добавьте в core/paths.py как os.path.join(TEMPLATES_DIR, "main_template.json")
MAIN_TEMPLATE = os.path.join(
    os.path.dirname(CONFIG_DIR), "templates", "main_template.json"
)
CENTRAL_CONFIG_PATH = CONFIG_JSON
APPS_CONFIG_DIR = os.path.join(CONFIG_DIR, "apps")
STORAGE_DIR = STORAGE_APPS_DIR

spinner_frames = ["/", "-", "\\", "|"]


# Шаблон main.json
def load_main_template():
    with open(MAIN_TEMPLATE, "r", encoding="utf-8") as f:
        return json.load(f)


# Папка для хранения результата по проекту
def create_project_folder(app_name, domain):
    storage_path = os.path.join(STORAGE_DIR, app_name, domain)
    os.makedirs(storage_path, exist_ok=True)
    return storage_path


# Сохранение main.json для проекта
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


# Асинх обработка одного партнера
async def process_partner(
    app_name,
    domain,
    url,
    main_template,
    prompts,
    ai_cfg,
    executor,
    allowed_categories,
    show_status=False,
    strapi_sync=None,
    api_url_proj=None,
    api_url_cat=None,
    api_token=None,
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
            executor, collect_main_data, url, main_template, storage_path
        )

        # временная заготовка для AI
        main_data_for_ai = dict(main_template)
        main_data_for_ai["name"] = domain.capitalize()
        main_data_for_ai.setdefault("socialLinks", {})
        main_data_for_ai["socialLinks"]["websiteURL"] = url

        # Параллельно только контент и coin
        ai_content_future = asyncio.create_task(
            ai_generate_content_markdown(
                main_data_for_ai, app_name, domain, prompts, ai_cfg, executor
            )
        )
        coin_future = asyncio.create_task(enrich_coin_async(main_data_for_ai, executor))

        # Асинхронно получаем все соцсети и общие данные по проекту
        main_data = await socials_future
        if isinstance(main_data, tuple):
            main_data = main_data[0]

        # подстраховка
        for k, v in main_template.items():
            if k not in main_data:
                main_data[k] = v

        # websiteURL в socialLinks
        main_data.setdefault("socialLinks", {})
        main_data["socialLinks"].setdefault("websiteURL", url)

        # Параллельно ждём AI контент и coin
        coin_result, content_md = await asyncio.gather(coin_future, ai_content_future)

        # Генерация шорт описания по content_md
        short_desc = await ai_generate_short_desc_with_retries(
            content_md, prompts, ai_cfg, executor
        )

        # Генерация категорий на основе content_md и allowed_categories
        categories = await ai_generate_project_categories(
            content_md, prompts, ai_cfg, executor, allowed_categories
        )

        # coinData, shortDescription, contentMarkdown
        if coin_result and "coinData" in coin_result:
            main_data["coinData"] = coin_result["coinData"]
        if short_desc:
            main_data["shortDescription"] = short_desc.strip()
        if content_md:
            main_data["contentMarkdown"] = content_md.strip()

        # Категории
        if not categories:
            main_data["project_categories"] = []
        elif strapi_sync and api_url_cat and api_token:
            category_ids = get_project_category_ids(api_url_cat, api_token, categories)
            main_data["project_categories"] = category_ids
        else:
            main_data["project_categories"] = categories

        # SEO
        main_data["seo"] = await build_seo_section(main_data, prompts, ai_cfg, executor)

        main_json_path = os.path.join(storage_path, "main.json")

        # Проверка и сохранение main.json
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


# Асинх обогащение данных по коину через CoinGecko
async def enrich_coin_async(main_data, executor):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, enrich_with_coin_id, main_data)


# Главная оркестрация всего пайплайна
async def orchestrate_all():
    # Глобальный список для fallback
    with open(CENTRAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        central_config = json.load(f)
    allowed_categories = central_config.get("categories", [])
    strapi_sync = central_config.get("strapi_sync", True)
    main_template = load_main_template()
    prompts = load_prompts()
    ai_cfg = load_ai_config()
    executor = ThreadPoolExecutor(max_workers=8)
    for app in central_config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        # Приоритет категорий
        app_categories = app.get("categories") or allowed_categories

        print(f"[app] {app_name} start")
        app_config_path = os.path.join(APPS_CONFIG_DIR, f"{app_name}.json")
        if not os.path.exists(app_config_path):
            logger.warning(f"Config for app {app_name} not found, skipping")
            continue
        with open(app_config_path, "r", encoding="utf-8") as f:
            app_config = json.load(f)
        for url in app_config["partners"]:
            api_url_proj = app.get("api_url_proj", "")
            api_url_cat = app.get("api_url_cat", "")
            api_token = app.get("api_token", "")
            domain = brand_from_url(url) or "project"
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
                        ai_cfg,
                        executor,
                        app_categories,
                        show_status=False,
                        strapi_sync=strapi_sync,
                        api_url_proj=api_url_proj,
                        api_url_cat=api_url_cat,
                        api_token=api_token,
                    ),
                    timeout=300,
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
            # strapi_sync: true - пуш в Strapi и печать статуса
            if not os.path.exists(main_json_path):
                print(f"[error] {app_name} - {url} - {elapsed} sec [No main.json]")
                continue
            with open(main_json_path, "r", encoding="utf-8") as fjson:
                main_data = json.load(fjson)
            if api_url_proj and api_token:
                from core.api.strapi import (
                    ERROR as STRAPI_ERROR,
                )
                from core.api.strapi import (
                    SKIP as STRAPI_SKIP,
                )
                from core.api.strapi import (
                    create_project,
                )

                status_strapi, project_id = create_project(
                    api_url_proj,
                    api_url_cat,
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
                        main_data, storage_path, api_url_proj, api_token, project_id
                    )
            else:
                print(f"[error] {app_name} - {url} - {elapsed} sec [No API]")
        print(f"[app] {app_name} done")


# Запуск пайплайна
def run_pipeline():
    asyncio.run(orchestrate_all())


if __name__ == "__main__":
    run_pipeline()
