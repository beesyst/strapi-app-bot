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
        strapi_log(f"[ERROR] project_exists: {e}")
    return None, None


# Создание или обновление проекта в Strapi
def create_project(api_url, api_token, data, app_name=None, domain=None, url=None):
    project_id, existing_attrs = project_exists(
        api_url, api_token, data.get("name", "")
    )
    # Если проект уже есть - сравнить поля, решить update/skip
    if project_id:
        status = check_strapi_status(data, existing_attrs)
        log_strapi_status(status, app_name, domain, url)
        if status == SKIP:
            strapi_log(f"[SKIP] Project exists: {data.get('name', '')}")
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
            "seo": data.get("seo", {}),
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
            f"[CREATE] {data.get('name', '')}: {resp.status_code}, {resp.text[:200]}"
        )
        if resp.status_code in (200, 201):
            return resp.json()["data"]["id"]
        # Попытка ещё раз проверить после ошибок (например, если уже создан)
        if resp.status_code in (409, 400, 500):
            project_id, _ = project_exists(api_url, api_token, data.get("name", ""))
            if project_id:
                log_strapi_status(SKIP, app_name, domain, url)
                strapi_log(f"[SKIP] Project exists after error: {data.get('name', '')}")
                return project_id
    except Exception as e:
        log_strapi_status(ERROR, app_name, domain, url, error_msg=str(e))
        strapi_log(f"[ERROR] create_project: {e}")
    return None


# Загрузка логотипа через эндпоинт /upload
def upload_logo(api_url, api_token, project_id, image_path):
    if not os.path.exists(image_path):
        strapi_log(f"[NO_IMAGE] {image_path}")
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
                f"[UPLOAD] {image_path} to project_id={project_id}: {resp.status_code}, {resp.text[:200]}"
            )
            if resp.status_code in (200, 201):
                return resp.json()[0]
    except Exception as e:
        strapi_log(f"[ERROR] upload_logo: {e}")
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
            strapi_log(f"[SKIP] {app_name}: no api_url or api_token")
            continue
        partners_path = os.path.join("config", "apps", f"{app_name}.json")
        if not os.path.exists(partners_path):
            strapi_log(f"[SKIP] {app_name}: no partners config")
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
                    f"[{app_name}] пустой domain для partner: {partner}, пропуск."
                )
                continue
            json_path = os.path.join("storage", "apps", app_name, domain, "main.json")
            image_path = os.path.join(
                "storage", "apps", app_name, domain, f"{domain}.jpg"
            )
            if not os.path.exists(json_path):
                strapi_log(f"[{app_name}] {domain}: main.json not found, skip.")
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
                strapi_log(f"[{app_name}] {domain}: main.json not loaded: {e}")
                continue
            project_id = create_project(
                api_url, api_token, data, app_name=app_name, domain=domain, url=partner
            )
            if not project_id:
                log_strapi_status(
                    ERROR, app_name, domain, partner, error_msg="project_id не создан"
                )
                strapi_log(f"[{app_name}] {domain}: project_id не создан")
                continue
            if data.get("svgLogo") and os.path.exists(image_path):
                upload_logo(api_url, api_token, project_id, image_path)
            else:
                strapi_log(f"[NO_IMAGE or NO_svgLogo]: {image_path}")


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
