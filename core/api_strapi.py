import json
import os

import markdown
import requests
from core.log_utils import strapi_log


# Перевод Markdown → HTML для отправки в Strapi
def markdown_to_html(md_text):
    return markdown.markdown(md_text, extensions=["extra"])


# Создание проекта в Strapi, возвращает project_id если успешно
def create_project(api_url, api_token, data):
    payload = {
        "data": {
            "name": data.get("name", ""),
            "shortDescription": data.get("shortDescription", ""),
            "socialLinks": data.get("socialLinks", {}),
            "contentMarkdown": markdown_to_html(data.get("contentMarkdown", "")),
            "coinData": data.get("coinData", {}),
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
    except Exception as e:
        strapi_log(f"[ERROR] create_project: {e}")
    return None


# Загрузка логотипа (jpeg, svgLogo) через эндпоинт /upload
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


# Основная функция синхронизации всех проектов (лог только в strapi.log, без терминала)
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
                strapi_log(f"[{app_name}] {domain}: main.json not loaded: {e}")
                continue
            project_id = create_project(api_url, api_token, data)
            if not project_id:
                strapi_log(f"[{app_name}] {domain}: project_id не создан")
                continue
            if data.get("svgLogo") and os.path.exists(image_path):
                upload_logo(api_url, api_token, project_id, image_path)
            else:
                strapi_log(f"[NO_IMAGE or NO_svgLogo]: {image_path}")


# Функция для синхронизации с выводом статусов в терминал (для orchestrator)
def sync_projects_with_terminal_status(config_path):
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
        partners_path = os.path.join("config", "apps", f"{app_name}.json")
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
            json_path = os.path.join("storage", "apps", app_name, domain, "main.json")
            image_path = os.path.join(
                "storage", "apps", app_name, domain, f"{domain}.jpg"
            )
            if not os.path.exists(json_path):
                print(f"[error] {app_name} - {partner}")
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f3:
                    data = json.load(f3)
                project_id = create_project(api_url, api_token, data)
                if project_id:
                    logo_uploaded = True
                    if data.get("svgLogo") and os.path.exists(image_path):
                        logo_result = upload_logo(
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


if __name__ == "__main__":
    sync_projects("config/config.json")
