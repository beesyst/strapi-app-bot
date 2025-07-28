import asyncio

from core.api_ai import (
    ai_generate_keywords,
)
from core.api_ai import (
    ai_generate_seo_desc as _ai_generate_seo_desc,
)
from core.log_utils import get_logger

# Логгер
logger = get_logger("seo_utils")
ai_logger = get_logger("ai")


# SEO-описание с ограничением длины и ретраями
async def ai_generate_seo_desc(
    short_desc, prompts, openai_cfg, executor, max_len=50, max_retries=3
):
    desc = await _ai_generate_seo_desc(
        short_desc, prompts, openai_cfg, executor, max_len=max_len
    )
    desc = (desc or "").strip()
    if len(desc) <= max_len:
        ai_logger.info("[seo_desc_first_try] %s", desc)
        return desc

    base_prompt = prompts["seo_short"].format(short_desc=short_desc, max_len=max_len)
    for i in range(max_retries):
        retry_prompt = prompts["seo_short_retry"].format(
            base_prompt=base_prompt, max_len=max_len, result=desc
        )
        ai_logger.info("[request] %s", retry_prompt[:200])
        desc_retry = await _ai_generate_seo_desc(
            retry_prompt, prompts, openai_cfg, executor, max_len=max_len
        )
        desc_retry = (desc_retry or "").strip()
        ai_logger.info("[seo_desc_retry #%d] %s (orig: %s)", i + 1, desc_retry, desc)
        if len(desc_retry) <= max_len:
            return desc_retry
        desc = desc_retry
    ai_logger.warning("[seo_desc_truncated] %s", desc[:max_len])
    return desc[:max_len]


# SEO-секция для main.json
async def build_seo_section(main_data, prompts, openai_cfg, executor):
    name = main_data.get("name") or ""
    short_desc = main_data.get("shortDescription") or ""
    content_md = main_data.get("contentMarkdown") or ""

    # SEO-описание и ключевые слова параллельно
    seo_desc_task = asyncio.create_task(
        ai_generate_seo_desc(short_desc, prompts, openai_cfg, executor, max_len=50)
    )
    keywords_task = asyncio.create_task(
        ai_generate_keywords(content_md, prompts, openai_cfg, executor)
    )
    seo_desc, keywords = await asyncio.gather(seo_desc_task, keywords_task)

    ai_logger.info(
        "[seo_result] metaTitle: '%s', metaDesc: '%s', seo_desc: '%s', keywords: '%s'",
        name,
        short_desc,
        seo_desc,
        keywords,
    )

    return {
        "metaTitle": name,
        "metaDescription": short_desc,
        "metaImage": None,
        "metaSocial": [
            {
                "socialNetwork": "Twitter",
                "title": name,
                "description": (seo_desc or "").strip(),
                "image": None,
            }
        ],
        "keywords": (keywords or "").strip(),
        "metaRobots": "",
    }
