import asyncio

from core.api.ai import (
    ai_generate_keywords,
    ai_generate_seo_desc_with_retries,
)
from core.log_utils import get_logger

logger = get_logger("seo_utils")


# SEO-секция для main.json
async def build_seo_section(main_data, prompts, ai_cfg, executor):
    name = main_data.get("name") or ""
    short_desc = main_data.get("shortDescription") or ""
    content_md = main_data.get("contentMarkdown") or ""

    # асинх получение seo_desc (через retries) и keywords
    seo_desc_task = asyncio.create_task(
        ai_generate_seo_desc_with_retries(short_desc, prompts, ai_cfg, executor)
    )
    keywords_task = asyncio.create_task(
        ai_generate_keywords(content_md, prompts, ai_cfg, executor)
    )
    seo_desc, keywords = await asyncio.gather(seo_desc_task, keywords_task)

    logger.info(
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
