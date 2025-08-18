import asyncio
import concurrent.futures
import json
import os

import requests
from core.log_utils import get_logger
from core.normalize import normalize_content_to_template_md_with_retry
from core.paths import (
    CONFIG_DIR,
    CONFIG_JSON,
    CONTENT_TEMPLATE,
    PROMPT_JSON,
    STORAGE_APPS_DIR,
)

# Константы
PROMPT_TYPE_REVIEW_FULL = "review_full"
PROMPT_TYPE_CONNECTION = "connection"
PROMPT_TYPE_FINALIZE = "finalize"
PROMPT_TYPE_SHORT_DESCRIPTION = "short_description"
PROMPT_TYPE_PROJECT_CATEGORIES = "project_categories"
PROMPT_TYPE_SEO_SHORT = "seo_short"
PROMPT_TYPE_SEO_KEYWORDS = "seo_keywords"

# Логгер
logger = get_logger("ai")


# Загрузка AI-конфига
def load_ai_config(config_path=CONFIG_JSON):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config["ai"]


# Поиск провайдера по имени модели
def find_provider_by_model(ai_cfg, model_name):
    for provider_name, prov_cfg in ai_cfg["providers"].items():
        if model_name in prov_cfg.get("models", []):
            return provider_name, prov_cfg
    raise ValueError(f"Provider for model '{model_name}' not found in config")


# Загрузка промптов из файла
def load_prompts(prompt_path=PROMPT_JSON):
    with open(prompt_path, "r", encoding="utf-8") as f:
        return json.load(f)


# Рендер шаблона промпта с контекстом
def render_prompt(template, context):
    return template.format(**context)


def get_group_for_prompt_type(ai_cfg, prompt_type):
    groups = ai_cfg["groups"]
    for group_cfg in groups.values():
        if "prompts" in group_cfg and prompt_type in group_cfg["prompts"]:
            return group_cfg
    raise ValueError(f"No group found for prompt_type '{prompt_type}'")


# Универсальный вызов AI API с полным конфигом
def call_ai_with_config(
    prompt, ai_cfg, custom_system_prompt=None, prompt_type="prompt"
):
    group_cfg = get_group_for_prompt_type(ai_cfg, prompt_type)
    model = group_cfg["model"]
    _, provider_cfg = find_provider_by_model(ai_cfg, model)

    # приоритет: api_url из группы → из провайдера
    api_url = group_cfg.get("api_url") or provider_cfg.get("api_url")
    api_key = provider_cfg.get("api_key")
    web_search_options = group_cfg.get("web_search_options")

    return call_ai_api(
        prompt=prompt,
        api_key=api_key,
        api_url=api_url,
        model=model,
        system_prompt=custom_system_prompt,
        prompt_type=prompt_type,
        web_search_options=web_search_options,
    )


# Прямой вызов AI API и лог результата
def call_ai_api(
    prompt,
    api_key,
    api_url,
    model,
    system_prompt=None,
    prompt_type="prompt",
    web_search_options=None,
):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # payload (для /responses и chat/completions)
    if api_url.endswith("/responses"):
        payload = {
            "model": model,
            "instructions": system_prompt if system_prompt else None,
            "input": [
                {"role": "user", "content": prompt},
            ],
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        if web_search_options:
            payload["tools"] = [{"type": "web_search", **web_search_options}]

        payload["tool_choice"] = "auto"
        payload["max_tool_calls"] = 6

    else:
        payload = {
            "model": model,
            "messages": (
                [{"role": "system", "content": system_prompt}] if system_prompt else []
            )
            + [{"role": "user", "content": prompt}],
        }
        if web_search_options:
            payload["web_search_options"] = web_search_options

    try:
        logger.info(f"[request] {prompt_type} prompt ({model}): {prompt}")
        logger.debug(f"[payload] {json.dumps(payload, ensure_ascii=False, indent=2)}")

        resp = requests.post(api_url, headers=headers, json=payload, timeout=180)

        if resp.status_code == 200:
            result = resp.json()
            text = ""

            if api_url.endswith("/responses"):
                if isinstance(result, dict) and "output" in result:
                    for item in result["output"]:
                        if item.get("type") == "message":
                            for block in item.get("content", []):
                                if block.get("type") == "output_text":
                                    text = block.get("text", "")
                                    break

                # Старый/альтернативный формат
                elif isinstance(result, list):
                    for item in result:
                        if item.get("type") == "message":
                            content = item.get("content", [])
                            if content and isinstance(content, list):
                                text = content[0].get("text", "")
                                break
                # В случае unexpected result - лог
                if not text:
                    logger.error(
                        "[error] No message found in responses result: %s",
                        str(result)[:1200],
                    )
                # Лог ответа (модель + полный текст)
                logger.info(f"[response] {prompt_type} ({model}): {text}")
                return text

            else:
                # Стандартный chat/completions
                choices = result.get("choices", [])
                if choices and "message" in choices[0]:
                    text = choices[0]["message"]["content"]
                else:
                    text = ""
                logger.info(f"[response] {prompt_type} ({model}): {text}")
                return text

        else:
            logger.error(
                "[error] status: %s, response: %s", resp.status_code, resp.text[:1200]
            )
    except Exception as e:
        logger.error("[EXCEPTION] %s", str(e))
    return ""


# Обновление contentMarkdown в main.json
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


# Обновление shortDescription в main.json
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


# Асинх генерация описания для проекта (short_desc)
async def ai_generate_short_desc(content, prompts, ai_cfg, executor):
    short_desc_cfg = ai_cfg["short_desc"]

    def sync_ai_short():
        context = {"content": content, "max_len": short_desc_cfg["max_len"]}
        short_prompt = render_prompt(prompts["short_description"], context)
        return call_ai_with_config(
            short_prompt, ai_cfg, prompt_type=PROMPT_TYPE_SHORT_DESCRIPTION
        )

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, sync_ai_short)
    return (result or "").strip()


# Асинхронная генерация short_description с ретраями
async def ai_generate_short_desc_with_retries(content, prompts, ai_cfg, executor):
    short_desc_cfg = ai_cfg["short_desc"]
    loop = asyncio.get_event_loop()

    def sync_short():
        context = {"content": content, "max_len": short_desc_cfg["max_len"]}
        short_prompt = render_prompt(prompts["short_description"], context)
        return call_ai_with_config(
            short_prompt, ai_cfg, prompt_type=PROMPT_TYPE_SHORT_DESCRIPTION
        )

    desc = await loop.run_in_executor(executor, sync_short)
    desc = (desc or "").strip()

    if len(desc) <= short_desc_cfg["strapi_limit"]:
        logger.info("[short_desc_first_try] %s", desc)
        return desc

    def sync_retry():
        context = {"content": content, "max_len": short_desc_cfg["retry_len"]}
        short_prompt_retry = render_prompt(prompts["short_description"], context)
        return call_ai_with_config(
            short_prompt_retry, ai_cfg, prompt_type=PROMPT_TYPE_SHORT_DESCRIPTION
        )

    desc_retry = await loop.run_in_executor(executor, sync_retry)
    desc_retry = (desc_retry or "").strip()

    if len(desc_retry) <= short_desc_cfg["strapi_limit"]:
        logger.info("[short_desc_retry] %s", desc_retry)
        return desc_retry

    cutoff = desc_retry[: short_desc_cfg["strapi_limit"]]
    if " " in cutoff:
        cutoff = cutoff[: cutoff.rfind(" ")]
    cutoff = cutoff.strip(" ,.;:-")
    logger.warning("[short_desc_truncated_by_space] %s", cutoff)
    return cutoff


def load_allowed_categories(config_path=CONFIG_JSON):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("categories", [])


# Нормализация и валидация категории из AI
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


# Асинх генерация массива категорий для проекта
async def ai_generate_project_categories(
    content, prompts, ai_cfg, executor, allowed_categories=None
):
    def sync_ai_categories():
        categories_str = ", ".join(allowed_categories or [])
        context = {"categories": categories_str, "content": content}
        prompt = render_prompt(prompts["project_categories"], context)
        raw = call_ai_with_config(
            prompt, ai_cfg, prompt_type=PROMPT_TYPE_PROJECT_CATEGORIES
        )
        if not raw:
            return []
        if "," in raw:
            cats = [c.strip() for c in raw.split(",") if c.strip()]
        else:
            cats = [c.strip("-•. \t") for c in raw.splitlines() if c.strip()]
        if allowed_categories is not None:
            return clean_categories(cats, allowed_categories)
        return cats[:3]

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_categories)


# Шаблон контента
def load_content_template(template_path=CONTENT_TEMPLATE):
    with open(template_path, "r", encoding="utf-8") as f:
        return json.load(f)


# Валидация секции связи
def ai_verify_connection_section(section_text, name1, name2, prompts, ai_cfg):
    context = {
        "name1": name1,
        "name2": name2,
        "text": section_text,
    }
    prompt = render_prompt(prompts["connection_verification"], context)
    result = call_ai_with_config(prompt, ai_cfg, prompt_type="connection_verification")
    return result.strip().upper() == "YES"


# Асинх генерация полного markdown-контент проекта
async def ai_generate_content_markdown(
    data, app_name, domain, prompts, ai_cfg, executor
):
    def sync_ai_content():
        # Генерация обзора
        context1 = {
            "name": data.get("name", domain),
            "website": data.get("socialLinks", {}).get("websiteURL", ""),
        }
        prompt1 = render_prompt(prompts["review_full"], context1)
        content1 = call_ai_with_config(
            prompt1, ai_cfg, prompt_type=PROMPT_TYPE_REVIEW_FULL
        )

        # Генерация связки
        main_app_config_path = os.path.join(CONFIG_DIR, "apps", f"{app_name}.json")
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
            content2 = call_ai_with_config(
                prompt2, ai_cfg, prompt_type=PROMPT_TYPE_CONNECTION
            )

        # Проверка
        all_content = content1
        connection_included = False
        if content2:
            is_valid = ai_verify_connection_section(
                content2, main_name, context1["name"], prompts, ai_cfg
            )
            if is_valid:
                all_content = (
                    f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
                )
                connection_included = True
            else:
                logger.info(
                    f"Connection section rejected by LLM for {main_name} x {context1['name']}"
                )
                all_content = content1
        else:
            all_content = content1

        # Финализация и перевод
        connection_title = (
            f"{main_name} x {context1['name']}" if connection_included else ""
        )
        context3 = {"connection_with": connection_title}
        finalize_instruction = render_prompt(prompts["finalize"], context3)

        final_content = call_ai_with_config(
            all_content,
            ai_cfg,
            custom_system_prompt=finalize_instruction,
            prompt_type=PROMPT_TYPE_FINALIZE,
        )

        content_template = load_content_template()

        # Функция-ретрай
        def ai_retry_func():
            context1 = {
                "name": data.get("name", domain),
                "website": data.get("socialLinks", {}).get("websiteURL", ""),
            }
            prompt1 = render_prompt(prompts["review_full"], context1)
            content1 = call_ai_with_config(
                prompt1, ai_cfg, prompt_type=PROMPT_TYPE_REVIEW_FULL
            )
            content2 = ""
            if domain.lower() != main_name.lower():
                context2 = {
                    "name1": main_name,
                    "website1": main_url,
                    "name2": context1["name"],
                    "website2": context1["website"],
                }
                prompt2 = render_prompt(prompts["connection"], context2)
                content2 = call_ai_with_config(
                    prompt2, ai_cfg, prompt_type=PROMPT_TYPE_CONNECTION
                )

            # Проверка-ретрай
            all_content = content1
            if content2:
                is_valid = ai_verify_connection_section(
                    content2, main_name, context1["name"], prompts, ai_cfg
                )
                if is_valid:
                    all_content = f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
                else:
                    logger.info(
                        f"Connection section rejected by LLM for {main_name} x {context1['name']}"
                    )
                    all_content = content1
            else:
                all_content = content1

            retry_connection_title = (
                f"{main_name} x {context1['name']}" if (content2 and is_valid) else ""
            )
            retry_context3 = {"connection_with": retry_connection_title}
            finalize_instruction = render_prompt(prompts["finalize"], retry_context3)

            final_content = call_ai_with_config(
                all_content,
                ai_cfg,
                custom_system_prompt=finalize_instruction,
                prompt_type=PROMPT_TYPE_FINALIZE,
            )
            return final_content

        normalized_md = normalize_content_to_template_md_with_retry(
            final_content,
            content_template,
            connection_title,
            ai_retry_func=ai_retry_func,
            max_retries=3,
        )
        return normalized_md.strip()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_ai_content)


# Асинх генерация SEO-описания
async def ai_generate_seo_desc(short_desc, prompts, ai_cfg, executor):
    seo_short_cfg = ai_cfg["seo_short"]

    def sync_seo_desc():
        context = {"short_desc": short_desc, "max_len": seo_short_cfg["max_len"]}
        prompt = render_prompt(prompts["seo_short"], context)
        return call_ai_with_config(prompt, ai_cfg, prompt_type=PROMPT_TYPE_SEO_SHORT)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, sync_seo_desc)
    return (result or "").strip()


# Асинх генерация seo_short с ретраями
async def ai_generate_seo_desc_with_retries(short_desc, prompts, ai_cfg, executor):
    seo_short_cfg = ai_cfg["seo_short"]
    loop = asyncio.get_event_loop()

    def sync_seo_1():
        context = {"short_desc": short_desc, "max_len": seo_short_cfg["max_len"]}
        prompt = render_prompt(prompts["seo_short"], context)
        return call_ai_with_config(prompt, ai_cfg, prompt_type=PROMPT_TYPE_SEO_SHORT)

    desc = await loop.run_in_executor(executor, sync_seo_1)
    desc = (desc or "").strip()

    if len(desc) <= seo_short_cfg["strapi_limit"]:
        logger.info("[seo_desc_first_try] %s", desc)
        return desc

    def sync_seo_2():
        context = {"short_desc": short_desc, "max_len": seo_short_cfg["retry_len"]}
        prompt = render_prompt(prompts["seo_short"], context)
        return call_ai_with_config(prompt, ai_cfg, prompt_type=PROMPT_TYPE_SEO_SHORT)

    desc_retry = await loop.run_in_executor(executor, sync_seo_2)
    desc_retry = (desc_retry or "").strip()

    if len(desc_retry) <= seo_short_cfg["strapi_limit"]:
        logger.info("[seo_desc_retry] %s", desc_retry)
        return desc_retry

    cutoff = desc_retry[: seo_short_cfg["strapi_limit"]]
    if " " in cutoff:
        cutoff = cutoff[: cutoff.rfind(" ")]
    cutoff = cutoff.strip(" ,.;:-")
    logger.warning("[seo_desc_truncated_by_space] %s", cutoff)
    return cutoff


# Асинх генерация SEO-ключевых слов
async def ai_generate_keywords(content, prompts, ai_cfg, executor):
    def sync_keywords():
        context = {"content": content or ""}
        prompt = render_prompt(prompts["seo_keywords"], context)
        return call_ai_with_config(prompt, ai_cfg, prompt_type=PROMPT_TYPE_SEO_KEYWORDS)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, sync_keywords)


# Синхр генерация для оффлайн-режима
async def process_all_projects(executor):
    ai_cfg = load_ai_config()
    prompts = load_prompts()
    base_dir = STORAGE_APPS_DIR
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

            if data.get("contentMarkdown"):
                logger.info("[SKIP] %s/%s: contentMarkdown уже есть", app_name, domain)
                continue

            # Генерация обзора
            context1 = {
                "name": data.get("name", domain),
                "website": data.get("socialLinks", {}).get("websiteURL", ""),
            }
            prompt1 = render_prompt(prompts["review_full"], context1)
            content1 = call_ai_with_config(
                prompt1, ai_cfg, prompt_type=PROMPT_TYPE_REVIEW_FULL
            )

            # Генерация связки
            main_app_config_path = os.path.join(CONFIG_DIR, "apps", f"{app_name}.json")
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
                content2 = call_ai_with_config(
                    prompt2, ai_cfg, prompt_type=PROMPT_TYPE_CONNECTION
                )

            # Проверка
            all_content = content1
            connection_included = False
            if content2:
                is_valid = ai_verify_connection_section(
                    content2, main_name, context1["name"], prompts, ai_cfg
                )
                if is_valid:
                    all_content = f"{content1}\n\n## {main_name} x {context1['name']}\n\n{content2}"
                    connection_included = True
                else:
                    logger.info(
                        f"Connection section rejected by LLM for {main_name} x {context1['name']}"
                    )

            connection_title = (
                f"{main_name} x {context1['name']}" if connection_included else ""
            )
            context3 = {"connection_with": connection_title}
            prompt3 = render_prompt(prompts["finalize"], context3)
            final_content = call_ai_with_config(
                all_content,
                ai_cfg,
                custom_system_prompt=prompt3,
                prompt_type=PROMPT_TYPE_FINALIZE,
            )

            content_template = load_content_template()
            normalized_md = normalize_content_to_template_md_with_retry(
                final_content,
                content_template,
                connection_title,
                ai_retry_func=None,
                max_retries=1,
            )

            if normalized_md:
                normalized_md = normalized_md.strip()
                enrich_main_json(json_path, normalized_md)

                # Генерация shortDescription
                short_desc = await ai_generate_short_desc_with_retries(
                    normalized_md, prompts, ai_cfg, executor
                )
                if short_desc:
                    enrich_short_description(json_path, short_desc)
                else:
                    logger.error(
                        "[fail] Не удалось сгенерировать shortDescription для %s/%s",
                        app_name,
                        domain,
                    )

            else:
                logger.error(
                    "[fail] Не удалось сгенерировать финальный контент для %s/%s",
                    app_name,
                    domain,
                )


if __name__ == "__main__":
    executor = concurrent.futures.ThreadPoolExecutor()
    asyncio.run(process_all_projects(executor))
