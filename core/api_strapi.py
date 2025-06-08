import json
import os

import requests


def sync_projects(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    for app in config["apps"]:
        api_url = app["api_url"]
        api_token = app["api_token"]
        for partner in app["partners"]:
            # Автоматически вычисляем путь до main.json
            domain = (
                partner.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
            )
            json_path = os.path.join(
                "strapiprojectsbot", "storage", "total", domain, "main.json"
            )
            if not os.path.exists(json_path):
                print(f"[{app['app']}] {domain}: main.json not found, skip.")
                continue
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            payload = {
                "data": {
                    "name": data.get("name", ""),
                    "svgLogo": data.get("svgLogo", ""),
                    "socialLinks": data.get("socialLinks", {}),
                    # Добавь остальные поля, если нужно
                }
            }
            headers = {"Authorization": api_token, "Content-Type": "application/json"}
            resp = requests.post(api_url, json=payload, headers=headers)
            print(
                f"[{app['app']}] {domain}: Status {resp.status_code} — {resp.text[:200]}"
            )


# Для запуска:
# sync_projects("strapiprojectsbot/config/config.json")
