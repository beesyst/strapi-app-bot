from __future__ import annotations

import re
import traceback

from core.log_utils import get_logger

# Нормализация
from core.normalize import (
    clean_project_name,
    force_https,
    is_bad_name,
    normalize_socials,
)

# Парс агрегаторов ссылок (перенесено в core/parser/)
from core.parser.link_aggregator import (
    is_link_aggregator,
)

# Twitter-парсинг/био/аватар (перенесено в core/parser/)
from core.parser.twitter import (
    download_twitter_avatar,
    get_links_from_x_profile,
    select_verified_twitter,
)

# Парс сайтов (перенесено в core/parser/)
from core.parser.web import extract_social_links, fetch_url_html, get_domain_name

# Пути проекта
from core.parser.youtube import (
    youtube_oembed_title,
    youtube_to_handle,
    youtube_watch_to_embed,
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
        for k in main_data["socialLinks"].keys():
            v = socials.get(k)
            if v:
                main_data["socialLinks"][k] = v

        # twitter: верификация (+ аватар) и доп.соцсети от агрегатора
        site_domain = get_domain_name(website_url)
        brand_token = site_domain.split(".")[0] if site_domain else ""
        twitter_final, enriched_from_agg, aggregator_url, avatar_verified = (
            "",
            {},
            "",
            "",
        )
        try:
            twitter_final, enriched_from_agg, aggregator_url, avatar_verified = (
                select_verified_twitter(
                    found_socials=main_data["socialLinks"],
                    socials=socials,
                    site_domain=site_domain,
                    brand_token=brand_token,
                    html=html,
                    url=website_url,
                    trust_home=False,
                )
            )
            if twitter_final:
                main_data["socialLinks"]["twitterURL"] = twitter_final

            # мерж соцссылок от агрегатора (кроме websiteURL)
            for k, v in (enriched_from_agg or {}).items():
                if not v or k == "websiteURL":
                    continue
                if k in main_data["socialLinks"] and not main_data["socialLinks"].get(
                    k
                ):
                    main_data["socialLinks"][k] = v

        except Exception as e:
            logger.warning("Ошибка верификации Twitter: %s", e)

        # bio + fallback аватар
        try:
            bio = {}
            avatar_url = avatar_verified or ""

            need_bio = False
            if main_data["socialLinks"].get("twitterURL"):
                if not avatar_url or not aggregator_url:
                    need_bio = True

            if need_bio:
                try:
                    bio = (
                        get_links_from_x_profile(
                            main_data["socialLinks"]["twitterURL"],
                            need_avatar=not bool(avatar_url),
                        )
                        or {}
                    )
                except Exception:
                    bio = {}

            # прямые ссылки и агрегатор из bio
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

            # если select_verified_twitter уже дал aggregator_url — bio-агрегатор не трогаем
            if (not aggregator_url) and aggregator_from_bio:
                try:
                    from core.parser.link_aggregator import (
                        extract_socials_from_aggregator,
                    )
                    from core.parser.link_aggregator import (
                        verify_aggregator_belongs as _verify_belongs,
                    )

                    tw = main_data["socialLinks"].get("twitterURL", "")
                    m = re.match(
                        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
                        (tw or "") + "/",
                        re.I,
                    )
                    h = m.group(1) if m else None

                    ok_belongs, verified_bits = _verify_belongs(
                        aggregator_from_bio, site_domain, h
                    )

                    if ok_belongs:
                        socials_from_agg = (
                            extract_socials_from_aggregator(aggregator_from_bio) or {}
                        )
                        for k, v in (socials_from_agg or {}).items():
                            if not v or k == "websiteURL":
                                continue
                            if k in main_data["socialLinks"] and not main_data[
                                "socialLinks"
                            ].get(k):
                                main_data["socialLinks"][k] = v

                        if verified_bits.get("websiteURL") and not main_data[
                            "socialLinks"
                        ].get("websiteURL"):
                            main_data["socialLinks"]["websiteURL"] = verified_bits[
                                "websiteURL"
                            ]

                        logger.info(
                            "BIO: агрегатор %s — соцссылки домёржены (text-match): %s",
                            aggregator_from_bio,
                            {k: v for k, v in main_data["socialLinks"].items() if v},
                        )
                    else:
                        logger.info(
                            "BIO: агрегатор %s — домен %s не найден в HTML, мёрж пропущен",
                            aggregator_from_bio,
                            site_domain,
                        )
                except Exception as e:
                    logger.warning(
                        "BIO: ошибка обработки агрегатора %s: %s",
                        aggregator_from_bio,
                        e,
                    )

            # аватар + файл
            if (
                avatar_url or (bio.get("avatar") if isinstance(bio, dict) else "")
            ) and main_data["socialLinks"].get("twitterURL"):
                real_avatar = avatar_url or bio.get("avatar") or ""
                if real_avatar:
                    project_slug = (
                        (site_domain.split(".")[0] or "project")
                        .replace(" ", "")
                        .lower()
                    )
                    logo_filename = f"{project_slug}.jpg"
                    saved = download_twitter_avatar(
                        avatar_url=real_avatar,
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

    except Exception as e:
        logger.error("collect_main_data CRASH: %s\n%s", e, traceback.format_exc())

    # финальная нормализация и https-фикс
    main_data["socialLinks"] = normalize_socials(main_data.get("socialLinks", {}))
    for k, v in list(main_data["socialLinks"].items()):
        if v:
            main_data["socialLinks"][k] = force_https(v)

    # единый итоговый лог со всеми источниками (веб, X, агрегатор)
    final_socials = {k: v for k, v in main_data["socialLinks"].items() if v}
    logger.info("Сайт %s: Итоговые соц-ссылки: %s", website_url, final_socials)

    return main_data


__all__ = ["collect_main_data"]
