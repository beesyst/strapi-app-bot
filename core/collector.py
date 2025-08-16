from __future__ import annotations

import json
import os
import traceback

from core.log_utils import get_logger

# Нормализация
from core.normalize import (
    clean_project_name,
    force_https,
    is_bad_name,
    normalize_socials,
)
from core.parser_link_aggregator import (
    extract_socials_from_aggregator,
    is_link_aggregator,
)
from core.parser_web import extract_social_links, fetch_url_html, get_domain_name

# Парс сайтов

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "config.json")
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

# YouTube
from core.parser_youtube import (
    youtube_oembed_title,
    youtube_to_handle,
    youtube_watch_to_embed,
)

# Twitter
from core.twitter_parser import (
    download_twitter_avatar,
    get_links_from_x_profile,
    select_verified_twitter,
)

# Логгер
logger = get_logger("collector")


# Основная функция для сбора соцсетей и docs по проекту
def collect_main_data(website_url: str, main_template: dict, storage_path: str) -> dict:
    # базовая структура = копия шаблона
    main_data = {k: v for k, v in main_template.items()}
    main_data.setdefault("name", "")
    main_data.setdefault("svgLogo", "")
    main_data.setdefault("videoSlider", [])
    main_data.setdefault(
        "socialLinks", {k: "" for k in main_template.get("socialLinks", {}).keys()}
    )
    main_data["socialLinks"]["websiteURL"] = website_url

    try:
        # главная страница сайта
        html = fetch_url_html(website_url, prefer="auto")

        # извлечение соцсетей с главной
        socials = extract_social_links(html, website_url, is_main_page=True)
        socials = normalize_socials(socials)
        logger.info(
            "Сайт %s: соц-ссылки (после normalize): %s",
            website_url,
            {k: v for k, v in socials.items() if v},
        )
        for k in main_data["socialLinks"].keys():
            v = socials.get(k)
            if v:
                main_data["socialLinks"][k] = v

        # twitter: верификация + bio
        site_domain = get_domain_name(website_url)
        brand_token = site_domain.split(".")[0] if site_domain else ""

        try:
            twitter_final, enriched_from_agg, aggregator_url = select_verified_twitter(
                found_socials=main_data["socialLinks"],
                socials=socials,
                site_domain=site_domain,
                brand_token=brand_token,
                html=html,
                url=website_url,
                max_internal_links=5,
                trust_home=False,
            )
            if twitter_final:
                main_data["socialLinks"]["twitterURL"] = twitter_final
            for k, v in (enriched_from_agg or {}).items():
                if (
                    v
                    and k in main_data["socialLinks"]
                    and not main_data["socialLinks"].get(k)
                ):
                    main_data["socialLinks"][k] = v

        except Exception as e:
            logger.warning("Ошибка верификации Twitter: %s", e)

        # bio + аватар
        try:
            bio = {}
            if main_data["socialLinks"].get("twitterURL"):
                bio = get_links_from_x_profile(
                    main_data["socialLinks"]["twitterURL"], need_avatar=True
                )
            # прямые ссылки и агрегаторы из bio
            aggregator_from_bio = ""
            for bio_url in bio.get("links") or []:
                host = bio_url.split("//")[-1].split("/")[0].lower().replace("www.", "")
                mapping = {
                    "x.com": "twitterURL",
                    "twitter.com": "twitterURL",
                    "t.me": "telegramURL",
                    "telegram.me": "telegramURL",
                    "discord.gg": "discordURL",
                    "discord.com": "discordURL",
                    "youtube.com": "youtubeURL",
                    "youtu.be": "youtubeURL",
                    "medium.com": "mediumURL",
                    "github.com": "githubURL",
                    "linkedin.com": "linkedinURL",
                    "reddit.com": "redditURL",
                }
                # агрегатор?
                if not aggregator_from_bio and is_link_aggregator(bio_url):
                    aggregator_from_bio = bio_url

                k = mapping.get(host)
                if (
                    k
                    and k in main_data["socialLinks"]
                    and not main_data["socialLinks"].get(k)
                ):
                    main_data["socialLinks"][k] = bio_url

            # если select_verified_twitter не дал агрегатор, а в bio он есть - обогатим через него
            if not (enriched_from_agg) and aggregator_from_bio:
                try:
                    logger.info(
                        "BIO: найден агрегатор %s — начинаю обогащение",
                        aggregator_from_bio,
                    )
                    socials_from_agg = (
                        extract_socials_from_aggregator(aggregator_from_bio) or {}
                    )
                    socials_from_agg = normalize_socials(socials_from_agg)
                    logger.info(
                        "BIO: агрегатор дал соц-ссылки: %s",
                        {k: v for k, v in socials_from_agg.items() if v},
                    )

                    agg_site = (
                        (socials_from_agg.get("websiteURL") or "").strip().lower()
                    )
                    if agg_site and site_domain and site_domain in agg_site:
                        logger.info(
                            "Aggregator %s подтверждает сайт %s — помечаем X как официальный",
                            aggregator_from_bio,
                            site_domain,
                        )
                        for k, v in socials_from_agg.items():
                            if (
                                v
                                and k in main_data["socialLinks"]
                                and not main_data["socialLinks"].get(k)
                            ):
                                main_data["socialLinks"][k] = v
                    else:
                        logger.info(
                            "Aggregator %s не подтвердил сайт (%s vs %s) — только мягкое обогащение",
                            aggregator_from_bio,
                            agg_site,
                            site_domain,
                        )
                        for k, v in socials_from_agg.items():
                            if (
                                v
                                and k in main_data["socialLinks"]
                                and not main_data["socialLinks"].get(k)
                            ):
                                main_data["socialLinks"][k] = v
                except Exception as e:
                    logger.warning(
                        "Ошибка разбора агрегатора из BIO (%s): %s",
                        aggregator_from_bio,
                        e,
                    )

            # аватар + файл
            avatar_url = bio.get("avatar", "")
            if avatar_url and main_data["socialLinks"].get("twitterURL"):
                project_slug = (
                    (site_domain.split(".")[0] or "project").replace(" ", "").lower()
                )
                logo_filename = f"{project_slug}.jpg"
                saved = download_twitter_avatar(
                    avatar_url=avatar_url,
                    twitter_url=main_data["socialLinks"]["twitterURL"],
                    storage_dir=storage_path,
                    filename=logo_filename,
                )
                if saved:
                    main_data["svgLogo"] = logo_filename
        except Exception as e:
            logger.warning("Ошибка BIO/аватара X: %s", e)

        # youtube обработка
        yt = main_data["socialLinks"].get("youtubeURL", "")
        if yt:
            try:
                embed = youtube_watch_to_embed(yt)
                if embed:
                    main_data["youtubeEmbed"] = embed
                handle = youtube_to_handle(yt)
                if handle:
                    main_data["youtubeHandle"] = handle
                title = youtube_oembed_title(yt)
                if title:
                    main_data["youtubeTitle"] = title
            except Exception as e:
                logger.warning("Ошибка обработки YouTube: %s", e)

        # имя проекта
        try:
            base = (site_domain or "").split(".")[0]
            name = clean_project_name(base.capitalize())
            if not is_bad_name(name):
                main_data["name"] = name
        except Exception as e:
            logger.warning("Ошибка очистки имени проекта: %s", e)

        # https fix
        for k, v in list(main_data.get("socialLinks", {}).items()):
            if v:
                main_data["socialLinks"][k] = force_https(v)

    except Exception as e:
        logger.error("collect_main_data CRASH: %s\n%s", e, traceback.format_exc())

    return main_data


__all__ = ["collect_main_data"]
