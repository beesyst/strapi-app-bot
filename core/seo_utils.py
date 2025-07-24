import asyncio

from core.api_ai import (
    ai_generate_keywords,
)
from core.api_ai import (
    ai_generate_seo_desc as _ai_generate_seo_desc,
)


# Генерация SEO-описания с ограничением длины и ретраями
async def ai_generate_seo_desc(
    short_desc, prompts, openai_cfg, executor, max_len=50, max_retries=3
):
    desc = await _ai_generate_seo_desc(
        short_desc, prompts, openai_cfg, executor, max_len=max_len
    )
    desc = (desc or "").strip()
    if len(desc) <= max_len:
        return desc

    # Retry loop если слишком длинно
    base_prompt = prompts["seo_short"].format(short_desc=short_desc, max_len=max_len)
    for _ in range(max_retries):
        retry_prompt = prompts["seo_short_retry"].format(
            base_prompt=base_prompt, max_len=max_len, result=desc
        )
        # Тут вызываем генератор с retry_prompt как short_desc!
        desc = await _ai_generate_seo_desc(
            retry_prompt, prompts, openai_cfg, executor, max_len=max_len
        )
        desc = (desc or "").strip()
        if len(desc) <= max_len:
            return desc
    return desc[:max_len]


# Генерация SEO-секции для main.json
async def build_seo_section(main_data, prompts, openai_cfg, executor):
    name = main_data.get("name") or ""
    short_desc = main_data.get("shortDescription") or ""
    content_md = main_data.get("contentMarkdown") or ""

    seo_desc_task = asyncio.create_task(
        ai_generate_seo_desc(short_desc, prompts, openai_cfg, executor, max_len=50)
    )
    keywords_task = asyncio.create_task(
        ai_generate_keywords(content_md, prompts, openai_cfg, executor)
    )
    seo_desc, keywords = await asyncio.gather(seo_desc_task, keywords_task)

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
