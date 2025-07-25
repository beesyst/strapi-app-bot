import json
import os

import markdown
import requests
from core.log_utils import strapi_log
from core.status import (
    ADD,
    ERROR,
    SKIP,
    check_strapi_status,
    log_strapi_status,
)


# Перевод Markdown → HTML для отправки в Strapi
def markdown_to_html(md_text):
    return markdown.markdown(md_text, extensions=["extra"])


# Проверка существования проекта в Strapi
def project_exists(api_url, api_token, name):
    url = f"{api_url}?filters[name][$eq]={name}"
    headers = {
        "Authorization": api_token,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data") and len(data["data"]) > 0:
                return data["data"][0]["id"], data["data"][0]["attributes"]
    except Exception as e:
        strapi_log(f"[error] project_exists: {e}", level="error")
    return None, None


# Лог всех секций main.json для strapi.log
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
        if key == "name" and val:
            strapi_log(f"[name] Размещено имя {val}")
        elif key == "svgLogo" and val:
            strapi_log(f"[svgLogo] {val}")
        elif key == "slug" and val:
            strapi_log(f"[slug] Создан слаг {val}")
        elif key == "coinData":
            coin = val.get("coin") if isinstance(val, dict) else ""
            if coin:
                strapi_log(f"[coinData] coin найден: {coin}")
            else:
                strapi_log("[coinData] coin не найден")
        elif isinstance(val, (dict, list)) and val:
            strapi_log(f"[{key}] Готово")
        elif val:
            strapi_log(f"[{key}] Готово")
        else:
            strapi_log(f"[{key}] не найдено", level="warning")


# Создание или обновление проекта в Strapi
def create_project(api_url, api_token, data, app_name=None, domain=None, url=None):
    project_id, existing_attrs = project_exists(
        api_url, api_token, data.get("name", "")
    )
    # Уже есть - сравниваем, решаем update/skip
    if project_id:
        status = check_strapi_status(data, existing_attrs)
        log_strapi_status(status, app_name, domain, url)
        if status == SKIP:
            strapi_log(f"[skip] Project exists: {data.get('name', '')}")
            log_strapi_sections(data)
            return project_id
    else:
        status = ADD
        log_strapi_status(status, app_name, domain, url)
    # Формируем payload для Strapi
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
        }
    }
    headers = {
        "Authorization": api_token,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
        strapi_log(
            f"[create] {data.get('name', '')}: {resp.status_code}, {resp.text[:200]}"
        )
        # Лог секций после отправки (чтобы было видно, что реально ушло)
        log_strapi_sections(data)
        if resp.status_code in (200, 201):
            return resp.json()["data"]["id"]
        # Попытка еще раз если ошибка (конфликт)
        if resp.status_code in (409, 400, 500):
            project_id, _ = project_exists(api_url, api_token, data.get("name", ""))
            if project_id:
                log_strapi_status(SKIP, app_name, domain, url)
                strapi_log(f"[skip] Project exists after error: {data.get('name', '')}")
                # Лог секций даже в этом кейсе
                log_strapi_sections(data)
                return project_id
    except Exception as e:
        log_strapi_status(ERROR, app_name, domain, url, error_msg=str(e))
        strapi_log(f"[error] create_project: {e}", level="error")
    return None


# Загрузка лого через эндпоинт /upload
def upload_logo(api_url, api_token, project_id, image_path):
    if not os.path.exists(image_path):
        strapi_log(f"[no_image] {image_path}", level="warning")
        return None
    upload_url = api_url.replace("/projects", "/upload")
    headers = {"Authorization": api_token}
    ref = "api::project.project"
    field = "svgLogo"
    try:
        with open(image_path, "rb") as f:
            files = {"files": (os.path.basename(image_path), f, "image/jpeg")}
            data = {"ref": ref, "refId": project_id, "field": field}
            resp = requests.post(upload_url, files=files, data=data, headers=headers)
            strapi_log(
                f"[svgLogo] {image_path} to project_id={project_id}: {resp.status_code}, {resp.text[:200]}"
            )
            if resp.status_code in (200, 201):
                strapi_log(f"[svgLogo] {image_path} to project_id={project_id}: OK")
                return resp.json()[0]
    except Exception as e:
        strapi_log(f"[error] upload_logo: {e}", level="error")
    return None


# Основная функция синхронизации всех проектов
def sync_projects(config_path, only_app=None):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        if only_app and app["app"] != only_app:
            continue
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        api_url = app.get("api_url")
        api_token = app.get("api_token")
        if not api_url or not api_token:
            strapi_log(f"[skip] {app_name}: no api_url or api_token", level="warning")
            continue
        partners_path = os.path.join("config", "apps", f"{app_name}.json")
        if not os.path.exists(partners_path):
            strapi_log(f"[skip] {app_name}: no partners config", level="warning")
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
                strapi_log(
                    f"[skip] пустой domain для partner: {partner}",
                    level="warning",
                )
                continue
            json_path = os.path.join("storage", "apps", app_name, domain, "main.json")
            image_path = os.path.join(
                "storage", "apps", app_name, domain, f"{domain}.jpg"
            )
            if not os.path.exists(json_path):
                strapi_log(
                    f"[skip] {domain}: main.json not found, skip.",
                    level="warning",
                )
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
                strapi_log(
                    f"[error] {domain}: main.json not loaded: {e}", level="error"
                )
                continue
            project_id = create_project(
                api_url, api_token, data, app_name=app_name, domain=domain, url=partner
            )
            if not project_id:
                log_strapi_status(
                    ERROR, app_name, domain, partner, error_msg="project_id не создан"
                )
                strapi_log(f"[error] {domain}: project_id не создан", level="error")
                continue
            if data.get("svgLogo") and os.path.exists(image_path):
                upload_logo(api_url, api_token, project_id, image_path)
            else:
                strapi_log(f"[no_image or no_svglogo]: {image_path}", level="warning")


# Альтернативная синхронизация без вывода в терминал
def sync_projects_with_terminal_status(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        if not app.get("enabled", True):
            continue
        app_name = app["app"]
        api_url = app.get("api_url", "")
        api_token = app.get("api_token", "")
        if not api_url or not api_token:
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
                project_id = create_project(
                    api_url, api_token, data, app_name, domain, partner
                )
                if project_id:
                    if data.get("svgLogo") and os.path.exists(image_path):
                        upload_logo(api_url, api_token, project_id, image_path)
            except Exception:
                continue


if __name__ == "__main__":
    sync_projects("config/config.json")
