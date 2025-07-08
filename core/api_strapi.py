import datetime
import json
import os

import requests

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
STRAPI_LOG = os.path.join(LOGS_DIR, "strapi.log")

# Очищаем strapi.log при каждом запуске
with open(STRAPI_LOG, "w", encoding="utf-8") as f:
    f.write("")


# Логгирование в файл
def strapi_log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STRAPI_LOG, "a", encoding="utf-8") as f:
        f.write(f"{now} {msg}\n")


# Создание проекта и возврат его ID (если успешно)
def create_project(api_url, api_token, data):
    payload = {
        "data": {
            "name": data.get("name", ""),
            "shortDescription": data.get("shortDescription", ""),
            "socialLinks": data.get("socialLinks", {}),
            "contentMarkdown": data.get("contentMarkdown", ""),
        }
    }
    headers = {
        "Authorization": api_token,
        "Content-Type": "application/json",
    }
    resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
    strapi_log(
        f"[CREATE] {data.get('name', '')}: {resp.status_code}, {resp.text[:200]}"
    )
    if resp.status_code in (200, 201):
        print(f"[ok] Created project: {data.get('name', '')}")
        return resp.json()["data"]["id"]  # Вернем id для дальнейшей привязки картинки
    else:
        print(
            f"[error] Create project: {data.get('name', '')} [{resp.status_code}]: {resp.text}"
        )
        return None


# Загрузка файла (лого) и привязка его к проекту через поле svgLogo
def upload_logo(api_url, api_token, project_id, image_path):
    if not os.path.exists(image_path):
        strapi_log(f"[no image]: {image_path}")
        return None
    upload_url = api_url.replace("/projects", "/upload")
    headers = {"Authorization": api_token}
    # Стандартный ref для коллекции projects: api::project.project
    ref = "api::project.project"
    field = "svgLogo"
    with open(image_path, "rb") as f:
        files = {"files": (os.path.basename(image_path), f, "image/jpeg")}
        data = {"ref": ref, "refId": project_id, "field": field}
        resp = requests.post(upload_url, files=files, data=data, headers=headers)
        strapi_log(
            f"[UPLOAD] {image_path} to project_id={project_id}: {resp.status_code}, {resp.text[:200]}"
        )
        if resp.status_code in (200, 201):
            return resp.json()[0]
        else:
            print(
                f"[error] Upload logo: {image_path} [{resp.status_code}]: {resp.text}"
            )
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
        api_url = app["api_url"]
        api_token = app["api_token"]
        if not api_url or not api_token:
            strapi_log(f"[skip] {app['app']}: no api_url or api_token")
            continue
        partners_path = os.path.join("config", "apps", f"{app['app']}.json")
        if not os.path.exists(partners_path):
            strapi_log(f"[skip] {app['app']}: no partners config")
            continue
        with open(partners_path, "r", encoding="utf-8") as f:
            partners_data = json.load(f)
        for partner in partners_data.get("partners", []):
            if not partner or not partner.strip():
                continue
            domain = (
                partner.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
            )
            if not domain:
                strapi_log(
                    f"[{app['app']}] пустой domain для partner: {partner}, пропуск."
                )
                continue
            json_path = os.path.join("storage", "total", domain, "main.json")
            image_path = os.path.join("storage", "total", domain, f"{domain}.jpg")
            if not os.path.exists(json_path):
                strapi_log(f"[{app['app']}] {domain}: main.json not found, skip.")
                continue
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            project_id = create_project(api_url, api_token, data)
            if not project_id:
                continue
            if data.get("svgLogo") and os.path.exists(image_path):
                upload_logo(api_url, api_token, project_id, image_path)
            else:
                strapi_log(f"[no image or svgLogo]: {image_path}")


if __name__ == "__main__":
    sync_projects("config/config.json")
