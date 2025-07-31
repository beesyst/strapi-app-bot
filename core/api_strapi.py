import json
import os

import markdown
import requests
from core.log_utils import get_logger
from core.status import (
    ADD,
    ERROR,
    SKIP,
    check_strapi_status,
    log_strapi_status,
)

# Логгер
logger = get_logger("strapi")


# Заголовок для Strapi API
def get_strapi_headers(api_token, extra=None, skip_content_type=False):
    headers = {
        "Authorization": f"Bearer {api_token}",
    }
    if not skip_content_type:
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    return headers


# Markdown → HTML для Strapi
def markdown_to_html(md_text):
    return markdown.markdown(md_text, extensions=["extra"])


# Проверка существования проекта в Strapi
def project_exists(api_url_proj, api_token, name):
    url = f"{api_url_proj}?filters[name][$eq]={name}"
    headers = get_strapi_headers(api_token)
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data") and len(data["data"]) > 0:
                return data["data"][0]["id"], data["data"][0]["attributes"]
    except Exception as e:
        logger.error(f"[project_exists] {e}")
    return None, None


# Лог ключевых секций main.json в strapi.log по секциям
def log_strapi_sections(data):
    sections = [
        "name",
        "svgLogo",
        "shortDescription",
        "project_categories",
        "socialLinks",
        "slug",
        "coinData",
        "seo",
        "contentMarkdown",
    ]
    for key in sections:
        val = data.get(key)
        # Форматированный лог по секциям
        if key == "name":
            if val:
                logger.info(f"[name] Размещено имя {val}")
            else:
                logger.warning("[name] не найдено")
        elif key == "svgLogo":
            if val:
                logger.info(f"[svgLogo] {val}")
            else:
                logger.warning("[svgLogo] не найдено")
        elif key == "shortDescription":
            if val:
                logger.info("[shortDescription] Готово")
            else:
                logger.warning("[shortDescription] не найдено")
        elif key == "project_categories":
            if val and isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], str):
                    logger.warning(
                        "[project_categories] не id, а строки! (нужно исправить)"
                    )
                else:
                    logger.info("[project_categories] Готово")
            else:
                logger.warning("[project_categories] не найдено")
        elif key == "socialLinks":
            if val and isinstance(val, dict) and any(val.values()):
                logger.info("[socialLinks] Готово")
            else:
                logger.warning("[socialLinks] не найдено")
        elif key == "slug":
            if val:
                logger.info(f"[slug] создан: {val}")
            else:
                logger.warning("[slug] не найдено")
        elif key == "coinData":
            coin_val = val.get("coin") if isinstance(val, dict) else ""
            if coin_val:
                logger.info(f"[coinData] coin найден: {coin_val}")
            else:
                logger.info("[coinData] coin не найден")
        elif key == "seo":
            if val and isinstance(val, dict) and any(val.values()):
                logger.info("[seo] Готово")
            else:
                logger.warning("[seo] не найдено")
        elif key == "contentMarkdown":
            if val:
                logger.info("[contentMarkdown] Готово")
            else:
                logger.warning("[contentMarkdown] не найдено")
        else:
            if val:
                logger.info(f"[{key}] Готово")
            else:
                logger.warning(f"[{key}] не найдено")


# Создание или обновление проекта в Strapi
def create_project(
    api_url_proj, api_url_cat, api_token, data, app_name=None, domain=None, url=None
):
    cats = data.get("project_categories", [])
    if cats and isinstance(cats[0], str):
        ids = get_project_category_ids(api_url_cat, api_token, cats)
        data["project_categories"] = ids
    project_id, existing_attrs = project_exists(
        api_url_proj, api_token, data.get("name", "")
    )
    if project_id:
        status = check_strapi_status(data, existing_attrs)
        log_strapi_status(status, app_name, domain, url)
        if status == SKIP:
            logger.info(f"[skip] Проект уже существует: {data.get('name', '')}")
            log_strapi_sections(data)
            return status, project_id
    else:
        status = ADD
        log_strapi_status(status, app_name, domain, url)
    payload = {
        "data": {
            "name": data.get("name", ""),
            "shortDescription": data.get("shortDescription", ""),
            "socialLinks": data.get("socialLinks", {}),
            "contentMarkdown": markdown_to_html(data.get("contentMarkdown", "")),
            "coinData": data.get("coinData", {}),
            "seo": data.get("seo") or {},
            "metaTitle": data.get("seo", {}).get("metaTitle", ""),
            "metaDescription": data.get("seo", {}).get("metaDescription", ""),
            "metaImage": data.get("seo", {}).get("metaImage", ""),
            "keywords": data.get("seo", {}).get("keywords", ""),
            "project_categories": data.get("project_categories", []),
        }
    }
    headers = get_strapi_headers(api_token)
    try:
        resp = requests.post(api_url_proj, json=payload, headers=headers, timeout=10)
        logger.info(
            f"[create] {data.get('name', '')}: {resp.status_code}, {resp.text[:200]}"
        )
        if resp.status_code in (200, 201):
            log_strapi_sections(data)
            return ADD, resp.json()["data"]["id"]
        if resp.status_code in (409, 400, 500):
            project_id, _ = project_exists(
                api_url_proj, api_token, data.get("name", "")
            )
            if project_id:
                log_strapi_status(SKIP, app_name, domain, url)
                logger.info(
                    f"[SKIP] Проект уже существует после ошибки: {data.get('name', '')}"
                )
                log_strapi_sections(data)
                return SKIP, project_id
    except Exception as e:
        log_strapi_status(ERROR, app_name, domain, url, error_msg=str(e))
        logger.error(f"[create_project] {e}")
    return ERROR, None


# Загрузка лого через эндпоинт /upload
def upload_logo(api_url, api_token, project_id, image_path):
    if not os.path.exists(image_path):
        logger.warning(f"[svgLogo] no_image: {image_path}")
        return None
    upload_url = api_url.replace("/projects", "/upload")
    headers = get_strapi_headers(api_token, skip_content_type=True)
    ref = "api::project.project"
    field = "svgLogo"
    try:
        with open(image_path, "rb") as f:
            files = {"files": (os.path.basename(image_path), f, "image/jpeg")}
            data = {"ref": ref, "refId": project_id, "field": field}
            resp = requests.post(upload_url, files=files, data=data, headers=headers)
            logger.info(
                f"[svgLogo] {image_path} to project_id={project_id}: {resp.status_code}, {resp.text[:200]}"
            )
            if resp.status_code in (200, 201):
                logger.info(f"[upload] {image_path} to project_id={project_id}: OK")
                return resp.json()[0]
    except Exception as e:
        logger.error(f"[upload] Ошибка загрузки svgLogo: {e}")
    return None


# Обновление seo секции с media id картинки (logo_id)
def update_seo_image(api_url_proj, api_token, project_id, logo_id):
    headers = get_strapi_headers(api_token)
    get_url = f"{api_url_proj}/{project_id}?populate[seo][populate][metaSocial]=*"
    resp = requests.get(get_url, headers=headers)
    if resp.status_code != 200:
        logger.warning(
            f"[seo_patch] Не удалось получить текущий seo: {resp.status_code}"
        )
        return False

    project = resp.json().get("data", {}).get("attributes", {})
    old_seo = project.get("seo", {}) if project else {}

    new_seo = dict(old_seo)
    new_seo["metaImage"] = logo_id
    if "metaSocial" in new_seo and new_seo["metaSocial"]:
        new_seo["metaSocial"][0]["image"] = logo_id
    else:
        new_seo["metaSocial"] = [{"socialNetwork": "Twitter", "image": logo_id}]

    data = {"seo": new_seo}
    put_url = f"{api_url_proj}/{project_id}"
    put_resp = requests.put(put_url, json={"data": data}, headers=headers)
    logger.info(f"[seo_patch] PATCH seo: {put_resp.status_code}, {put_resp.text[:200]}")
    return put_resp.status_code == 200


# Загрузка svgLogo проекта в Strapi
def try_upload_logo(main_data, storage_path, api_url, api_token, project_id):
    image_name = main_data.get("svgLogo")
    if not image_name:
        logger.warning("[svgLogo] не найдено (нет поля svgLogo)")
        return None
    image_path = os.path.join(storage_path, image_name)
    if not os.path.exists(image_path):
        logger.warning(f"[svgLogo] не найдено (файл отсутствует): {image_path}")
        return None
    result = upload_logo(api_url, api_token, project_id, image_path)
    if result:
        logger.info(
            f"[svgLogo] успешно загружено: {image_name} для project_id={project_id}"
        )
        logo_id = result.get("id")
        if logo_id:
            update_seo_image(api_url, api_token, project_id, logo_id)
        return result
    else:
        logger.warning(f"[svgLogo] ошибка загрузки: {image_name}")
        return None


# Получение или создание категории по имени
def get_or_create_project_category(api_url_cat, api_token, category_name):
    url = f"{api_url_cat}?filters[name][$eq]={category_name}"
    headers = get_strapi_headers(api_token)
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        items = data.get("data", [])
        if items:
            return items[0]["id"]
    create_url = api_url_cat
    payload = {"data": {"name": category_name}}
    resp = requests.post(create_url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        data = resp.json()
        return data["data"]["id"]
    return None


# Список id (или [])
def get_project_category_ids(api_url_cat, api_token, category_names):
    ids = []
    for cat in category_names:
        id_ = get_or_create_project_category(api_url_cat, api_token, cat)
        if id_:
            ids.append(id_)
    return ids


# Основная функция синхронизации всех проектов по шаблону
def sync_projects(config_path, only_app=None):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        if only_app and app["app"] != only_app:
            continue
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        api_url_proj = app.get("api_url_proj")
        api_url_cat = app.get("api_url_cat")
        api_token = app.get("api_token")
        if not api_url_proj or not api_url_cat or not api_token:
            logger.warning(
                f"[skip] {app_name}: no api_url_proj or api_url_cat or api_token"
            )
            continue
        partners_path = os.path.join("config", "apps", f"{app_name}.json")
        if not os.path.exists(partners_path):
            logger.warning(f"[skip] {app_name}: no partners config")
            continue
        with open(partners_path, "r", encoding="utf-8") as f2:
            partners_data = json.load(f2)
        for partner in partners_data.get("partners", []):
            if not partner or not partner.strip():
                continue
            domain = (
                partner.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
            )
            if not domain:
                logger.warning(f"[skip] пустой domain для partner: {partner}")
                continue
            json_path = os.path.join("storage", "apps", app_name, domain, "main.json")
            image_path = os.path.join(
                "storage", "apps", app_name, domain, f"{domain}.jpg"
            )
            if not os.path.exists(json_path):
                logger.warning(f"[skip] {domain}: main.json not found, skip.")
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f3:
                    data = json.load(f3)
            except Exception as e:
                log_strapi_status(
                    ERROR,
                    app_name,
                    domain,
                    partner,
                    error_msg=f"main.json not loaded: {e}",
                )
                logger.error(f"[main.json] не загружен для {domain}: {e}")
                continue
            status, project_id = create_project(
                app.get("api_url_proj"),
                app.get("api_url_cat"),
                api_token,
                data,
                app_name=app_name,
                domain=domain,
                url=partner,
            )
            if status == ERROR or not project_id:
                log_strapi_status(
                    ERROR, app_name, domain, partner, error_msg="project_id не создан"
                )
                logger.error(f"[project_id] не создан: {domain}")
                continue
            try_upload_logo(
                data,
                os.path.dirname(json_path),
                app.get("api_url_proj"),
                api_token,
                project_id,
            )


# Альтернативная синхронизация без вывода в терминал
def sync_projects_with_terminal_status(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        api_url_proj = app.get("api_url_proj")
        api_url_cat = app.get("api_url_cat")
        api_token = app.get("api_token")
        if not api_url_proj or not api_url_cat or not api_token:
            continue
        partners_path = os.path.join("config", "apps", f"{app_name}.json")
        if not os.path.exists(partners_path):
            continue
        with open(partners_path, "r", encoding="utf-8") as f2:
            partners_data = json.load(f2)
        for partner in partners_data.get("partners", []):
            if not partner or not partner.strip():
                continue
            domain = (
                partner.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
            )
            json_path = os.path.join("storage", "apps", app_name, domain, "main.json")
            image_path = os.path.join(
                "storage", "apps", app_name, domain, f"{domain}.jpg"
            )
            if not os.path.exists(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f3:
                    data = json.load(f3)
                status, project_id = create_project(
                    app.get("api_url_proj"),
                    app.get("api_url_cat"),
                    api_token,
                    data,
                    app_name=app_name,
                    domain=domain,
                    url=partner,
                )
                if status not in (ERROR, None) and project_id:
                    if data.get("svgLogo") and os.path.exists(image_path):
                        upload_logo(
                            app.get("api_url_proj"), api_token, project_id, image_path
                        )
            except Exception:
                continue


if __name__ == "__main__":
    sync_projects("config/config.json")
