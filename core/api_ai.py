import asyncio
import json
import os

import requests
from core.log_utils import get_logger

# Получаем логгер для AI-компонента
logger = get_logger("ai")


# Загрузка OpenAI-конфига
def load_openai_config(config_path="config/config.json"):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config["openai"]


# Загрузка промптов из файла
def load_prompts(prompt_path="config/prompt.json"):
    with open(prompt_path, "r", encoding="utf-8") as f:
        return json.load(f)


# Рендер шаблона промпта с контекстом
def render_prompt(template, context):
    return template.format(**context)


# Универсальный вызов OpenAI API с полным конфигом
def call_ai_with_config(prompt, openai_cfg):
    return call_openai_api(
        prompt,
        openai_cfg["api_key"],
        openai_cfg["api_url"],
        openai_cfg["model"],
        openai_cfg["system_prompt"],
        openai_cfg["temperature"],
        openai_cfg["max_tokens"],
    )


# Прямой вызов OpenAI API и логирование результата
def call_openai_api(
    prompt, api_key, api_url, model, system_prompt, temperature, max_tokens
):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
        logger.info("[request] prompt: %s...", prompt[:150])
        if resp.status_code == 200:
            result = resp.json()
            text = result["choices"][0]["message"]["content"]
            logger.info("[response] %s...", text[:150])
            return text
        else:
            logger.error(
                "[error] status: %s, response: %s", resp.status_code, resp.text[:500]
            )
    except Exception as e:
        logger.error("[EXCEPTION] %s", str(e))
    return ""


# Обновляет contentMarkdown в main.json
def enrich_main_json(json_path, content):
    if not os.path.exists(json_path):
        logger.error("[ERROR] main.json not found: %s", json_path)
        return False
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["contentMarkdown"] = content
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("[OK] contentMarkdown обновлён для %s", json_path)
    return True


# Обновляет shortDescription в main.json
def enrich_short_description(json_path, short_desc):
    if not os.path.exists(json_path):
        logger.error("[ERROR] main.json не найден: %s", json_path)
        return False
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["shortDescription"] = short_desc.strip()
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("[OK] shortDescription обновлён для %s", json_path)
    return True


# Асинхронно генерирует короткое описание для проекта (short_desc)
async def ai_generate_short_desc(data, prompts, openai_cfg, executor):
    def sync_ai_short():
        short_ctx = {
            "name2": data.get("name", ""),
            "website2": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        short_prompt = render_prompt(prompts["short_description"], short_ctx)
        return call_ai_with_config(short_prompt, openai_cfg)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_short)


def load_allowed_categories(config_path="config/config.json"):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("categories", [])


# Нормализует и валидирует категории из AI
def clean_categories(raw_cats, allowed_categories):
    if not isinstance(raw_cats, list):
        raw_cats = [c.strip() for c in raw_cats.split(",") if c.strip()]
    allowed = {c.lower(): c for c in allowed_categories}
    result = []
    for c in raw_cats:
        key = c.strip().lower()
        if key in allowed and allowed[key] not in result:
            result.append(allowed[key])
    return result[:3]


# Асинхронно генерирует массив категорий для проекта
async def ai_generate_project_categories(
    data, prompts, openai_cfg, executor, allowed_categories=None
):
    def sync_ai_categories():
        context = {
            "name1": data.get("name", ""),
            "website1": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        prompt = render_prompt(prompts["project_categories"], context)
        raw = call_ai_with_config(prompt, openai_cfg)
        if not raw:
            return []
        if "," in raw:
            cats = [c.strip() for c in raw.split(",") if c.strip()]
        else:
            cats = [c.strip("-•. \t") for c in raw.splitlines() if c.strip()]
        # Магия clean_categories
        if allowed_categories is not None:
            return clean_categories(cats, allowed_categories)
        return cats[:3]

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_categories)


# Асинхронно генерирует полный markdown-контент проекта
async def ai_generate_content_markdown(
    data, app_name, domain, prompts, openai_cfg, executor
):
    def sync_ai_content():
        context1 = {
            "name": data.get("name", domain),
            "website": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        prompt1 = render_prompt(prompts["review_full"], context1)
        content1 = call_ai_with_config(prompt1, openai_cfg)
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
            content2 = call_ai_with_config(prompt2, openai_cfg)
        all_content = content1
        if content2:
            all_content = (
                f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
            )
        context3 = {"connection_with": main_name if content2 else ""}
        prompt3 = render_prompt(prompts["finalize"], context3)
        final_content = call_ai_with_config(f"{all_content}\n\n{prompt3}", openai_cfg)
        return final_content

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_content)


# Асинхронный генератор SEO-описания
async def ai_generate_seo_desc(short_desc, prompts, openai_cfg, executor, max_len=50):
    def sync_seo_desc():
        context = {"short_desc": short_desc, "max_len": max_len}
        prompt = render_prompt(prompts["seo_short"], context)
        return call_ai_with_config(prompt, openai_cfg)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_seo_desc)


# Асинхронный генератор SEO-ключевых слов
async def ai_generate_keywords(content, prompts, openai_cfg, executor):
    def sync_keywords():
        context = {"content": content or ""}
        prompt = render_prompt(prompts["seo_keywords"], context)
        return call_ai_with_config(prompt, openai_cfg)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_keywords)


# Синхронный генератор для оффлайн-режима
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
                logger.info("[SKIP] %s/%s: contentMarkdown уже есть", app_name, domain)
                continue

            # shortDescription
            if not data.get("shortDescription"):
                short_ctx = {
                    "name2": data.get("name", domain),
                    "website2": data.get("socialLinks", {}).get("websiteURL", ""),
                }
                short_prompt = render_prompt(prompts["short_description"], short_ctx)
                short_desc = call_ai_with_config(short_prompt, openai_cfg)
                if short_desc:
                    enrich_short_description(json_path, short_desc)
                else:
                    logger.error(
                        "[FAIL] Не удалось сгенерировать shortDescription для %s/%s",
                        app_name,
                        domain,
                    )

            # Основной markdown-обзор
            context1 = {
                "name": data.get("name", domain),
                "website": data.get("socialLinks", {}).get("websiteURL", ""),
            }
            prompt1 = render_prompt(prompts["review_full"], context1)
            content1 = call_ai_with_config(prompt1, openai_cfg)

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
                content2 = call_ai_with_config(prompt2, openai_cfg)

            # Финализация и перевод
            all_content = content1
            if content2:
                all_content = (
                    f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
                )
            context3 = {"connection_with": main_name if content2 else ""}
            prompt3 = render_prompt(prompts["finalize"], context3)
            final_content = call_ai_with_config(
                f"{all_content}\n\n{prompt3}", openai_cfg
            )

            if final_content:
                enrich_main_json(json_path, final_content)
            else:
                logger.error(
                    "[FAIL] Не удалось сгенерировать финальный контент для %s/%s",
                    app_name,
                    domain,
                )


if __name__ == "__main__":
    process_all_projects()
