from core.api.ai import ai_generate_keywords, ai_generate_seo_desc_with_retries
from core.log_utils import get_logger

logger = get_logger("seo_utils")


# SEO-секция для main.json
async def build_seo_section(main_data, prompts, ai_cfg, executor):
    name = (main_data.get("name") or "").strip()
    short_desc = (main_data.get("shortDescription") or "").strip()
    content_md = (main_data.get("contentMarkdown") or "").strip()

    # лимит для social description берем из config.json -> ai_cfg["seo_short"]["strapi_limit"]
    social_limit = int((ai_cfg.get("seo_short") or {}).get("strapi_limit", 60))

    # генерация ИИ с ретраями (max_len -> retry_len).
    desc_for_social = ""
    if short_desc:
        try:
            desc_for_social = await ai_generate_seo_desc_with_retries(
                short_desc, prompts, ai_cfg, executor
            )
            # защита от редких случаев, когда модель все равно вылезла за лимит
            if len(desc_for_social) > social_limit:
                logger.warning(
                    "[seo_short_guard] model returned over-limit after retries (len=%d, limit=%d)",
                    len(desc_for_social),
                    social_limit,
                )
                desc_for_social = ""
        except Exception as e:
            logger.warning("[seo_short] generation failed: %s", e)
            desc_for_social = ""

    # keywords - best-effort и не ломают пайплайн
    keywords = ""
    try:
        if content_md:
            kw = await ai_generate_keywords(content_md, prompts, ai_cfg, executor)
            keywords = (kw or "").strip()
    except Exception as e:
        logger.warning("[seo_keywords] generation failed: %s", e)
        keywords = ""

    logger.info(
        "[seo_result] built for '%s' (short_len=%d, social_len=%d, kw_len=%d)",
        name,
        len(short_desc),
        len(desc_for_social),
        len(keywords),
    )

    return {
        "metaTitle": name,
        "metaDescription": short_desc,
        "metaImage": None,
        "metaSocial": [
            {
                "socialNetwork": "Twitter",
                "title": name,
                "description": desc_for_social,
                "image": None,
            }
        ],
        "keywords": keywords,
        "metaRobots": "",
    }
