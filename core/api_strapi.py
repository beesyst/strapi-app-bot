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


def strapi_log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STRAPI_LOG, "a", encoding="utf-8") as f:
        f.write(f"{now} {msg}\n")


def sync_projects(config_path, only_app=None):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        if only_app and app["app"] != only_app:
            continue


def sync_projects(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
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
            domain = (
                partner.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
            )
            json_path = os.path.join("storage", "total", domain, "main.json")
            if not os.path.exists(json_path):
                strapi_log(f"[{app['app']}] {domain}: main.json not found, skip.")
                continue
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            payload = {
                "data": {
                    "name": data.get("name", ""),
                    "shortDescription": data.get("shortDescription", ""),
                    "socialLinks": data.get("socialLinks", {}),
                }
            }
            headers = {
                "Authorization": api_token,
                "Content-Type": "application/json",
            }
            strapi_log(f"[REQUEST] {app['app']} {domain}: POST {api_url}")
            strapi_log(f"   > Payload: {json.dumps(payload, ensure_ascii=False)}")
            try:
                resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
                strapi_log(f"   > Response [{resp.status_code}]: {resp.text[:200]}")
                if resp.status_code in (200, 201):
                    print(f"[ok] {app['app']} - {partner}")
                else:
                    print(
                        f"[error] {app['app']} - {partner} [{resp.status_code}]: {resp.text}"
                    )
            except Exception as e:
                strapi_log(f"   > ERROR: {e}")
                print(f"[error] {app['app']} - {partner}: {e}")


if __name__ == "__main__":
    sync_projects("config/config.json")
