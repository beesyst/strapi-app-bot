import asyncio
import json
import os

import requests
from core.log_utils import ai_log


# Загрузка OpenAI-конфига
def load_openai_config(config_path="config/config.json"):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config["openai"]


# Загрузка промптов
def load_prompts(prompt_path="config/prompt.json"):
    with open(prompt_path, "r", encoding="utf-8") as f:
        return json.load(f)


# Рендер промпта с контекстом (форматирование шаблона)
def render_prompt(template, context):
    return template.format(**context)


# Запрос к OpenAI API
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
        ai_log(f"[AI][REQUEST] prompt: {prompt[:150]}...")
        if resp.status_code == 200:
            result = resp.json()
            text = result["choices"][0]["message"]["content"]
            ai_log(f"[AI][RESPONSE] {text[:150]}...")
            return text
        else:
            ai_log(
                f"[AI][ERROR] status: {resp.status_code}, response: {resp.text[:500]}"
            )
    except Exception as e:
        ai_log(f"[AI][EXCEPTION] {str(e)}")
    return ""


# enrich_main_json: обновляет contentMarkdown в main.json
def enrich_main_json(json_path, content):
    if not os.path.exists(json_path):
        ai_log(f"[AI][ERROR] main.json not found: {json_path}")
        return False
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["contentMarkdown"] = content
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    ai_log(f"[AI][OK] contentMarkdown обновлён для {json_path}")
    return True


# enrich_short_description: обновляет shortDescription в main.json
def enrich_short_description(json_path, short_desc):
    if not os.path.exists(json_path):
        ai_log(f"[AI][ERROR] main.json не найден: {json_path}")
        return False
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["shortDescription"] = short_desc.strip()
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    ai_log(f"[AI][OK] shortDescription обновлён для {json_path}")
    return True


# Асинхронный генератор короткого описания для orchestrator
async def ai_generate_short_desc(data, prompts, openai_cfg, executor):
    def sync_ai_short():
        short_ctx = {
            "name2": data.get("name", ""),
            "website2": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        short_prompt = render_prompt(prompts["short_description"], short_ctx)
        return call_openai_api(
            short_prompt,
            openai_cfg["api_key"],
            openai_cfg["api_url"],
            openai_cfg["model"],
        )

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_short)


# Асинхронный генератор полного markdown-контента для orchestrator
async def ai_generate_content_markdown(
    data, app_name, domain, prompts, openai_cfg, executor
):
    def sync_ai_content():
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
        # Проверяем связь проектов
        main_app_config_path = os.path.join("config", "apps", f"{app_name}.json")
        if os.path.exists(main_app_config_path):
            with open(main_app_config_path, "r", encoding="utf-8") as f:
                main_app_cfg = json.load(f)
            main_name = main_app_cfg.get("name", app_name.capitalize())
            main_url = main_app_cfg.get("url", "")
        else:
            main_name = app_name.capitalize()
            main_url = ""
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
        return final_content

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_content)


# Основной (синхронный) пайплайн генерации для оффлайн-режима
def process_all_projects():
    openai_cfg = load_openai_config()
    prompts = load_prompts()
    base_dir = os.path.join("storage", "apps")

    for app_name in os.listdir(base_dir):
        app_path = os.path.join(base_dir, app_name)
        if not os.path.isdir(app_path):
            continue

        for domain in os.listdir(app_path):
            partner_path = os.path.join(app_path, domain)
            if not os.path.isdir(partner_path):
                continue

            json_path = os.path.join(partner_path, "main.json")
            if not os.path.exists(json_path):
                continue

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Пропускаем, если уже есть сгенерированный контент
            if data.get("contentMarkdown"):
                ai_log(f"[AI][SKIP] {app_name}/{domain}: contentMarkdown уже есть")
                continue

            # Короткое описание (shortDescription)
            if not data.get("shortDescription"):
                short_ctx = {
                    "name2": data.get("name", domain),
                    "website2": data.get("socialLinks", {}).get("websiteURL", ""),
                }
                short_prompt = render_prompt(prompts["short_description"], short_ctx)
                short_desc = call_openai_api(
                    short_prompt,
                    openai_cfg["api_key"],
                    openai_cfg["api_url"],
                    openai_cfg["model"],
                )
                if short_desc:
                    enrich_short_description(json_path, short_desc)
                else:
                    ai_log(
                        f"[AI][FAIL] Не удалось сгенерировать shortDescription для {app_name}/{domain}"
                    )

            # Основной markdown-обзор
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

            content2 = ""
            # Добавляем связку, если имя не совпадает с основным проектом
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

            # Финализация и перевод
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
                    f"[AI][FAIL] Не удалось сгенерировать финальный контент для {app_name}/{domain}"
                )


if __name__ == "__main__":
    process_all_projects()
