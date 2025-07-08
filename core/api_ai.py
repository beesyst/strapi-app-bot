import datetime
import json
import os

import requests

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
AI_LOG = os.path.join(LOGS_DIR, "ai.log")


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
    base_dir = os.path.join("storage", "total")
    projects = [
        p for p in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, p))
    ]
    # Получаем список name/website по всем проектам (для связок)
    project_data = {}
    for p in projects:
        json_path = os.path.join(base_dir, p, "main.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            project_data[p] = {
                "name": data.get("name", p),
                "website": data.get("socialLinks", {}).get("websiteURL", ""),
            }

    for p in projects:
        json_path = os.path.join(base_dir, p, "main.json")
        if not os.path.exists(json_path):
            continue
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("contentMarkdown"):
            ai_log(f"[SKIP] {p}: contentMarkdown уже есть")
            continue
        # Генерируем основной обзор
        context1 = {
            "name": data.get("name", p),
            "website": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        prompt1 = render_prompt(prompts["review_full"], context1)
        content1 = call_openai_api(
            prompt1, openai_cfg["api_key"], openai_cfg["api_url"], openai_cfg["model"]
        )

        # Если есть второй проект — генерация связи
        other_key = None
        for k in project_data:
            if k != p:
                other_key = k
                break
        content2 = ""
        if other_key:
            context2 = {
                "name1": context1["name"],
                "website1": context1["website"],
                "name2": project_data[other_key]["name"],
                "website2": project_data[other_key]["website"],
            }
            prompt2 = render_prompt(prompts["connection"], context2)
            content2 = call_openai_api(
                prompt2,
                openai_cfg["api_key"],
                openai_cfg["api_url"],
                openai_cfg["model"],
            )
        # Финализация — комбинируем, форматируем
        all_content = content1
        if content2:
            all_content = f"{content1}\n\n## {context2['name2']} x {context1['name']}\n\n{content2}"

        context3 = {"connection_with": context2["name2"] if other_key else ""}
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
            ai_log(f"[FAIL] Не удалось сгенерировать финальный контент для {p}")


if __name__ == "__main__":
    process_all_projects()
