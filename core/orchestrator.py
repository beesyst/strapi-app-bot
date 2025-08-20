import asyncio
import json
import multiprocessing as mp
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

# Пути
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
    if os.environ.get("DISABLE_CHILD_SPINNER") == "1":
        return
    idx = 0
    while not stop_event.is_set():
        spin = spinner_frames[idx % len(spinner_frames)]
        print(f"\r[{spin}] {text} ", end="", flush=True)
        idx += 1
        time.sleep(0.13)


# Воркер для процесса
def _partner_worker(
    queue,
    app_name,
    domain,
    url,
    main_template,
    prompts,
    ai_cfg,
    allowed_categories,
    strapi_sync,
    api_url_proj,
    api_url_cat,
    api_token,
    ai_active,
):
    # в дочернем процессе свой executor и свой event loop
    os.environ["DISABLE_CHILD_SPINNER"] = "1"
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        status = asyncio.run(
            process_partner(
                app_name,
                domain,
                url,
                main_template,
                prompts,
                ai_cfg,
                executor,
                allowed_categories,
                show_status=False,
                strapi_sync=strapi_sync,
                api_url_proj=api_url_proj,
                api_url_cat=api_url_cat,
                api_token=api_token,
                ai_active=ai_active,
                spinner_event=None,
            )
        )
    except Exception:
        status = ERROR
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)
    # отдаем статус родителю
    try:
        queue.put(status)
    except Exception:
        pass


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
    ai_active=True,
    spinner_event=None,
):
    start_time = time.time()
    spinner_text = f"{app_name} - {url}"

    owns_spinner = False
    if spinner_event is None and os.environ.get("DISABLE_CHILD_SPINNER") != "1":
        stop_event = threading.Event()
        spinner_thread = threading.Thread(
            target=spinner_task, args=(spinner_text, stop_event)
        )
        spinner_thread.start()
        owns_spinner = True
    else:
        stop_event = threading.Event() if spinner_event is None else spinner_event
        spinner_thread = None

    status = ERROR
    try:
        storage_path = create_project_folder(app_name, domain)
        logger.info(f"Создание main.json - {app_name} - {url}")
        logger.info(f"Сбор соц линков - {app_name} - {url}")
        if ai_active:
            logger.info(f"ИИ-генерация - {app_name} - {url}")
        else:
            logger.info(
                f"ИИ-генерация отключена (ai_active=False) - {app_name} - {url}"
            )
        logger.info(f"Поиск API ID в Coingecko - {app_name} - {url}")

        loop = asyncio.get_event_loop()

        # сбор соцлинков / основных данных
        socials_future = loop.run_in_executor(
            executor, collect_main_data, url, main_template, storage_path
        )

        # заготовка данных для параллельных задач
        main_data_for_ai = dict(main_template)
        main_data_for_ai["name"] = domain.capitalize()
        main_data_for_ai.setdefault("socialLinks", {})
        main_data_for_ai["socialLinks"]["websiteURL"] = url

        # запуск coinGecko
        coin_future = asyncio.create_task(enrich_coin_async(main_data_for_ai, executor))

        # контент по ИИ - только если ai_active
        if ai_active:
            ai_content_future = asyncio.create_task(
                ai_generate_content_markdown(
                    main_data_for_ai, app_name, domain, prompts, ai_cfg, executor
                )
            )
            # асинх получение всех соцсетей
            main_data = await socials_future
            if isinstance(main_data, tuple):
                main_data = main_data[0]

            # подстраховка по ключам шаблона
            for k, v in main_template.items():
                if k not in main_data:
                    main_data[k] = v

            # websiteURL в socialLinks
            main_data.setdefault("socialLinks", {})
            main_data["socialLinks"].setdefault("websiteURL", url)

            # асинх ожидание ИИ контент и coin
            coin_result = await coin_future

            # контент пытаемся получить с таймаутом и безопасно
            content_md = ""
            try:
                CONTENT_TIMEOUT = int(os.environ.get("CONTENT_TIMEOUT_SEC", "240"))
                content_md = await asyncio.wait_for(
                    ai_content_future, timeout=CONTENT_TIMEOUT
                )
                content_md = (content_md or "").strip()
            except Exception as e:
                logger.warning("[content_llm] failed or timed out: %s", e)
                content_md = ""

            # short_desc и категории считаем только если есть контент
            short_desc = ""
            categories = []
            if content_md:
                try:
                    short_desc = await ai_generate_short_desc_with_retries(
                        content_md, prompts, ai_cfg, executor
                    )
                    short_desc = (short_desc or "").strip()
                except Exception as e:
                    logger.warning("[short_desc] generation failed: %s", e)

                try:
                    categories = await ai_generate_project_categories(
                        content_md, prompts, ai_cfg, executor, allowed_categories
                    )
                except Exception as e:
                    logger.warning("[categories] generation failed: %s", e)

        else:
            # ИИ выключен - socials и coin, без генерации контента
            main_data = await socials_future
            if isinstance(main_data, tuple):
                main_data = main_data[0]

            # подстраховка по ключам шаблона
            for k, v in main_template.items():
                if k not in main_data:
                    main_data[k] = v

            # websiteURL в socialLinks
            main_data.setdefault("socialLinks", {})
            main_data["socialLinks"].setdefault("websiteURL", url)

            # ожидание coinGecko
            coin_result = await coin_future
            content_md = ""  # без ИИ - нет markdown-контента
            short_desc = ""  # без ИИ - нет ген краткого описания
            categories = []  # без ИИ - нет подбора категории

        # записываем даныне в main_data
        if coin_result and "coinData" in coin_result:
            main_data["coinData"] = coin_result["coinData"]
        if short_desc:
            main_data["shortDescription"] = short_desc.strip()
        if content_md:
            main_data["contentMarkdown"] = content_md.strip()

        # категории → id (если strapi_sync и есть доступ к api категорий)
        if not categories:
            main_data["project_categories"] = []
        elif strapi_sync and api_url_cat and api_token:
            category_ids = get_project_category_ids(api_url_cat, api_token, categories)
            main_data["project_categories"] = category_ids
        else:
            main_data["project_categories"] = categories

        # строим seo
        if main_data.get("shortDescription") or main_data.get("contentMarkdown"):
            main_data["seo"] = await build_seo_section(
                main_data, prompts, ai_cfg, executor
            )
        else:
            main_data["seo"] = {}

        main_json_path = os.path.join(storage_path, "main.json")

        # проверка и сохранение main.json
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

        if status in (ADD, UPDATE):
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
        if owns_spinner and spinner_thread:
            spinner_thread.join()
        time.sleep(0.01)
    return status


# Асинх обогащение данных по коину через CoinGecko
async def enrich_coin_async(main_data, executor):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, enrich_with_coin_id, main_data)


# Главная оркестрация всего пайплайна
async def orchestrate_all():
    # глобальный список для fallback
    with open(CENTRAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        central_config = json.load(f)

    allowed_categories = central_config.get("categories", [])

    # новый контейнер настроек strapi с бэксовместимостью
    strapi_cfg = central_config.get("strapi", {})
    strapi_sync = strapi_cfg.get("strapi_sync", central_config.get("strapi_sync", True))
    strapi_publish_cfg = strapi_cfg.get("strapi_publish", True)

    main_template = load_main_template()
    prompts = load_prompts()
    ai_cfg = load_ai_config()

    from core.api.ai import is_ai_enabled

    ai_active = is_ai_enabled(ai_cfg)

    will_publish = bool(strapi_publish_cfg and ai_active)

    # таймаут на одного партнера (сек)
    PARTNER_TIMEOUT = int(central_config.get("partner_timeout_sec", 400))

    try:
        ctx = mp.get_context("fork")
    except ValueError:
        ctx = mp.get_context("spawn")

    for app in central_config["apps"]:
        if not app.get("enabled", True):
            continue

        app_name = app["app"]
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

            # родительский спиннер (одна строка в терминале)
            spinner_text = f"{app_name} - {url}"
            ext_stop_event = threading.Event()
            ext_spinner_thread = threading.Thread(
                target=spinner_task, args=(spinner_text, ext_stop_event)
            )
            ext_spinner_thread.start()

            # дочерний процесс + очередь для возврата статуса
            q = ctx.Queue()
            p = ctx.Process(
                target=_partner_worker,
                args=(
                    q,
                    app_name,
                    domain,
                    url,
                    main_template,
                    prompts,
                    ai_cfg,
                    app_categories,
                    strapi_sync,
                    api_url_proj,
                    api_url_cat,
                    api_token,
                    ai_active,
                ),
            )
            p.start()

            # ждем завершения с таймаутом
            p.join(PARTNER_TIMEOUT)

            if p.is_alive():
                # таймаут: останавливаем красивый спиннер, печатаем error, жестко убиваем воркер
                ext_stop_event.set()
                ext_spinner_thread.join()

                print(f"\r[error] {app_name} - {url} - timeout!", end="", flush=True)
                print()

                # жесткая рубка
                p.terminate()
                p.join()
                continue

            # воркер завершился сам - забираем его статус
            try:
                status_main = q.get_nowait()
            except Exception:
                status_main = "ok" if (p.exitcode == 0) else ERROR

            # останавливаем спиннер перед печатью финала
            ext_stop_event.set()
            ext_spinner_thread.join()

            elapsed = int(time.time() - start_time)

            if not strapi_sync:
                extra = " [main.json Error]" if status_main == ERROR else ""
                print(
                    f"\r[{status_main}] {app_name} - {url} - {elapsed} sec{extra}",
                    end="",
                    flush=True,
                )
                print()
                continue

            if not os.path.exists(main_json_path):
                print(f"[error] {app_name} - {url} - {elapsed} sec [No main.json]")
                continue

            with open(main_json_path, "r", encoding="utf-8") as fjson:
                main_data = json.load(fjson)

            if api_url_proj and api_token:
                from core.api.strapi import ERROR as STRAPI_ERROR
                from core.api.strapi import SKIP as STRAPI_SKIP
                from core.api.strapi import create_project

                publish_flag = bool(will_publish)
                try:
                    status_strapi, project_id = create_project(
                        api_url_proj,
                        api_url_cat,
                        api_token,
                        main_data,
                        app_name=app_name,
                        domain=domain,
                        url=url,
                        publish=publish_flag,
                    )
                except TypeError:
                    status_strapi, project_id = create_project(
                        api_url_proj,
                        api_url_cat,
                        api_token,
                        main_data,
                        app_name=app_name,
                        domain=domain,
                        url=url,
                    )

                if status_strapi == STRAPI_ERROR:
                    final_status = "error"
                    badges = ["Strapi Create Failed"]
                elif status_strapi == STRAPI_SKIP:
                    final_status = "skip"
                    badges = ["Already exists"]
                else:
                    # успех (создано/обновлено)
                    final_status = status_strapi
                    badges = ["Published" if publish_flag else "Draft"]

                extra = f" [{' | '.join(badges)}]" if badges else ""

                print(
                    f"\r[{final_status}] {app_name} - {url} - {elapsed} sec{extra}",
                    end="",
                    flush=True,
                )
                print()

                if project_id and status_strapi != STRAPI_ERROR:
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
