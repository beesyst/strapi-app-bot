import datetime
import json
import os

import requests

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
AI_LOG = os.path.join(LOGS_DIR, "ai.log")

# Очищаем ai.log при каждом запуске
with open(AI_LOG, "w", encoding="utf-8") as f:
    f.write("")


# Логирование
def ai_log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(AI_LOG, "a", encoding="utf-8") as f:
        f.write(f"{now} {msg}\n")


# Загрузка конфига OpenAI
def load_openai_config(config_path="config/config.json"):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config["openai"]


# Загрузка всех промптов
def load_prompts(prompt_path="config/prompt.json"):
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompts = json.load(f)
    return prompts


# Подстановка значений в промпт через .format()
def render_prompt(template, context):
    return template.format(**context)


# Запрос к OpenAI
def call_openai_api(prompt, api_key, api_url, model):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Ты профессиональный крипто-журналист."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 2048,
    }
    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
        ai_log(f"[REQUEST] prompt: {prompt[:150]}...")
        if resp.status_code == 200:
            result = resp.json()
            text = result["choices"][0]["message"]["content"]
            ai_log(f"[RESPONSE] {text[:150]}...")
            return text
        else:
            ai_log(f"[ERROR] status: {resp.status_code}, response: {resp.text[:500]}")
            return ""
    except Exception as e:
        ai_log(f"[EXCEPTION] {str(e)}")
        return ""


# Сохраняет сгенерированный текст в contentMarkdown main.json
def enrich_main_json(json_path, content):
    if not os.path.exists(json_path):
        ai_log(f"[ERROR] main.json not found: {json_path}")
        return False
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["contentMarkdown"] = content
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    ai_log(f"[OK] contentMarkdown обновлён для {json_path}")
    return True


# Основной пайплайн генерации
def process_all_projects():
    openai_cfg = load_openai_config()
    prompts = load_prompts()
    base_dir = os.path.join("storage", "apps")

    # Для каждого приложения (например, celestia, solana и т.д.)
    for app_name in os.listdir(base_dir):
        app_path = os.path.join(base_dir, app_name)
        if not os.path.isdir(app_path):
            continue

        # Для каждого партнера внутри приложения
        for domain in os.listdir(app_path):
            partner_path = os.path.join(app_path, domain)
            if not os.path.isdir(partner_path):
                continue

            json_path = os.path.join(partner_path, "main.json")
            if not os.path.exists(json_path):
                continue

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("contentMarkdown"):
                ai_log(f"[SKIP] {app_name}/{domain}: contentMarkdown уже есть")
                continue

            # Генерируем основной обзор
            context1 = {
                "name": data.get("name", domain),
                "website": data.get("socialLinks", {}).get("websiteURL", ""),
            }
            prompt1 = render_prompt(prompts["review_full"], context1)
            content1 = call_openai_api(
                prompt1,
                openai_cfg["api_key"],
                openai_cfg["api_url"],
                openai_cfg["model"],
            )

            # Связь с главным проектом (например, Celestia x Astria)
            main_app_config_path = os.path.join("config", "apps", f"{app_name}.json")
            if os.path.exists(main_app_config_path):
                with open(main_app_config_path, "r", encoding="utf-8") as f:
                    main_app_cfg = json.load(f)
                main_name = main_app_cfg.get("name", app_name.capitalize())
                main_url = main_app_cfg.get("url", "")
            else:
                main_name = app_name.capitalize()
                main_url = ""

            # Если имя партнера совпадает с основным, не делаем связку
            content2 = ""
            if domain.lower() != main_name.lower():
                context2 = {
                    "name1": main_name,
                    "website1": main_url,
                    "name2": context1["name"],
                    "website2": context1["website"],
                }
                prompt2 = render_prompt(prompts["connection"], context2)
                content2 = call_openai_api(
                    prompt2,
                    openai_cfg["api_key"],
                    openai_cfg["api_url"],
                    openai_cfg["model"],
                )

            # Финализация — форматируем и переводим
            all_content = content1
            if content2:
                all_content = (
                    f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
                )

            context3 = {"connection_with": main_name if content2 else ""}
            prompt3 = render_prompt(prompts["finalize"], context3)
            final_content = call_openai_api(
                f"{all_content}\n\n{prompt3}",
                openai_cfg["api_key"],
                openai_cfg["api_url"],
                openai_cfg["model"],
            )
            if final_content:
                enrich_main_json(json_path, final_content)
            else:
                ai_log(
                    f"[FAIL] Не удалось сгенерировать финальный контент для {app_name}/{domain}"
                )


if __name__ == "__main__":
    process_all_projects()
