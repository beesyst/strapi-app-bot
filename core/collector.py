from __future__ import annotations

import copy
import re
import traceback

from core.api.coingecko import enrich_with_coin_id
from core.log_utils import get_logger
from core.normalize import (
    force_https,
    normalize_socials,
)
from core.parser.link_aggregator import is_link_aggregator
from core.parser.twitter import (
    download_twitter_avatar,
    get_links_from_x_profile,
    reset_verified_state,
    select_verified_twitter,
)
from core.parser.web import (
    extract_project_name,
    extract_social_links,
    fetch_url_html,
    get_domain_name,
)
from core.parser.youtube import (
    youtube_oembed_title,
    youtube_to_handle,
    youtube_watch_to_embed,
)

# Логгер
logger = get_logger("collector")


# Основная функция для сбора соцсетей и docs по проекту
def collect_main_data(website_url: str, main_template: dict, storage_path: str) -> dict:
    reset_verified_state(full=True)

    main_data = copy.deepcopy(main_template)
    social_keys = list((main_template.get("socialLinks") or {}).keys())
    main_data["socialLinks"] = {k: "" for k in social_keys}
    main_data["socialLinks"]["websiteURL"] = website_url
    main_data["videoSlider"] = []
    main_data["svgLogo"] = ""
    main_data.setdefault("name", "")
    main_data.setdefault("shortDescription", "")
    main_data.setdefault("contentMarkdown", "")
    main_data.setdefault("seo", {})
    main_data.setdefault("coinData", {})

    try:
        # главная страница сайта
        html = fetch_url_html(website_url, prefer="http")

        # извлекаем соцсети с главной
        socials = extract_social_links(html, website_url, is_main_page=True)
        socials = normalize_socials(socials)

        # перезаполняем только найденными значениями (пустые - не трогаем)
        for k in social_keys:
            v = socials.get(k)
            if isinstance(v, str) and v.strip():
                main_data["socialLinks"][k] = v.strip()

        # Coingecko: обогащение coinData + соцсетей токена
        try:
            main_data = enrich_with_coin_id(main_data)
        except Exception as e:
            logger.warning("CoinGecko обогащение не удалось: %s", e)

        # twitter: верификация/агрегаторы/аватар
        site_domain = get_domain_name(website_url)
        brand_token = site_domain.split(".")[0] if site_domain else ""
        twitter_final = ""
        twitter_verified_url = ""
        enriched_from_agg = {}
        aggregator_url = ""
        avatar_verified = ""

        try:
            _res = select_verified_twitter(
                found_socials=main_data["socialLinks"],
                socials=socials,
                site_domain=site_domain,
                brand_token=brand_token,
                html=html,
                url=website_url,
                trust_home=False,
            )
            # аккуратно разбираем разные варианты кортежа
            if isinstance(_res, tuple):
                if len(_res) == 4:
                    (
                        twitter_final,
                        enriched_from_agg,
                        aggregator_url,
                        avatar_verified,
                    ) = _res
                elif len(_res) == 3:
                    twitter_final, enriched_from_agg, aggregator_url = _res
                elif len(_res) >= 1:
                    twitter_final = _res[0]

            # twitter_final считаем "подтвержденным" twitterURL
            if twitter_final:
                twitter_verified_url = twitter_final
                main_data["socialLinks"]["twitterURL"] = twitter_final

            # мержим соцсети из агрегатора
            for k, v in (enriched_from_agg or {}).items():
                if k == "websiteURL" or not v:
                    continue
                if k in main_data["socialLinks"] and not main_data["socialLinks"][k]:
                    main_data["socialLinks"][k] = v

        except Exception as e:
            logger.warning("Ошибка верификации Twitter: %s", e)

        # bio/аватар + возможный агрегатор из био
        try:
            bio = {}

            # аватарка из подтвержденного профиля (по домену/агрегатору)
            avatar_url = avatar_verified or ""

            # twitterURL считаем "разрешенным" только если он был подтверждён
            twitter_verified_url = twitter_verified_url or ""
            has_verified_twitter = bool(twitter_verified_url)

            # нужен ли дополнительный запрос BIO для вытаскивания аватарки
            need_bio_for_avatar = bool(has_verified_twitter and not avatar_url)

            # имя из X тянем всегда из подтвержденного профиля (need_avatar=False)
            twitter_display = ""
            if has_verified_twitter:
                try:
                    tw_profile = (
                        get_links_from_x_profile(
                            twitter_verified_url,
                            need_avatar=False,
                        )
                        or {}
                    )
                    twitter_display = (tw_profile.get("name") or "").strip()
                except Exception:
                    twitter_display = ""

            # если аватар не подтвержден, дергаем профиль с need_avatar=True
            if need_bio_for_avatar and has_verified_twitter:
                try:
                    bio = (
                        get_links_from_x_profile(
                            twitter_verified_url,
                            need_avatar=True,
                        )
                        or {}
                    )
                except Exception:
                    bio = {}

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
                # обнаружили ссылку на агрегатор - запомним
                if not aggregator_from_bio and is_link_aggregator(bio_url):
                    aggregator_from_bio = bio_url

                k = mapping.get(host)
                if k in main_data["socialLinks"] and not main_data["socialLinks"][k]:
                    main_data["socialLinks"][k] = bio_url

            if (not aggregator_url) and aggregator_from_bio:
                try:
                    from core.parser.link_aggregator import (
                        extract_socials_from_aggregator,
                    )
                    from core.parser.link_aggregator import (
                        verify_aggregator_belongs as _verify_belongs,
                    )

                    # handle берем из подтвержденного twitterURL
                    tw = twitter_verified_url
                    m = re.match(
                        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
                        (tw or "") + "/",
                        re.I,
                    )
                    handle = m.group(1) if m else None

                    ok_belongs, verified_bits = _verify_belongs(
                        aggregator_from_bio, site_domain, handle
                    )

                    if ok_belongs:
                        socials_from_agg = (
                            extract_socials_from_aggregator(aggregator_from_bio) or {}
                        )
                        for k, v in socials_from_agg.items():
                            if k == "websiteURL" or not v:
                                continue
                            if (
                                k in main_data["socialLinks"]
                                and not main_data["socialLinks"][k]
                            ):
                                main_data["socialLinks"][k] = v

                        if verified_bits.get("websiteURL") and not main_data[
                            "socialLinks"
                        ].get("websiteURL"):
                            main_data["socialLinks"]["websiteURL"] = verified_bits[
                                "websiteURL"
                            ]

                        logger.info(
                            "BIO: агрегатор %s - соцссылки домержены (text-match): %s",
                            aggregator_from_bio,
                            {k: v for k, v in main_data["socialLinks"].items() if v},
                        )
                    else:
                        logger.info(
                            "BIO: агрегатор %s - домен %s не найден в HTML, мерж пропущен",
                            aggregator_from_bio,
                            site_domain,
                        )
                except Exception as e:
                    logger.warning(
                        "BIO: ошибка обработки агрегатора %s: %s",
                        aggregator_from_bio,
                        e,
                    )

            # финальный выбор аватара
            real_avatar = avatar_verified or (
                bio.get("avatar") if isinstance(bio, dict) else ""
            )

            if real_avatar and has_verified_twitter:
                project_slug = (
                    (site_domain.split(".")[0] or "project").replace(" ", "").lower()
                )
                logo_filename = f"{project_slug}.jpg"
                saved = download_twitter_avatar(
                    avatar_url=real_avatar,
                    twitter_url=twitter_verified_url,
                    storage_dir=storage_path,
                    filename=logo_filename,
                )
                if saved:
                    main_data["svgLogo"] = logo_filename
        except Exception as e:
            logger.warning("Ошибка BIO/аватара X: %s", e)

        # youtube (по желанию)
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

        # имя проекта - пробрасываем twitter_display_name
        try:
            parsed_name = extract_project_name(
                html,
                website_url,
                twitter_display_name=(
                    twitter_display if "twitter_display" in locals() else ""
                ),
            )
            if parsed_name:
                main_data["name"] = parsed_name
        except Exception as e:
            logger.warning(
                "Ошибка определения имени проекта (web.extract_project_name): %s", e
            )

    except Exception as e:
        logger.error("collect_main_data CRASH: %s\n%s", e, traceback.format_exc())

    # финальная нормализация + https
    main_data["socialLinks"] = normalize_socials(main_data.get("socialLinks", {}))
    for k, v in list(main_data["socialLinks"].items()):
        if isinstance(v, str) and v:
            main_data["socialLinks"][k] = force_https(v)

    logger.info(
        "Конечный результат %s: %s",
        website_url,
        {k: v for k, v in main_data["socialLinks"].items() if v},
    )

    return main_data


__all__ = ["collect_main_data"]
