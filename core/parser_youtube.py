import json
import re
from urllib.parse import quote as urlquote

import requests
from core.log_utils import get_logger
from core.normalize import force_https

# Логгер
logger = get_logger("parser_youtube")


# Привод youtube-ссылки к каноническому виду @handle или /channel/...
def youtube_to_handle(url: str) -> str:
    u = force_https(url)
    if not u or not isinstance(u, str):
        return u

    # handle
    if re.search(r"^https://(www\.)?youtube\.com/@[A-Za-z0-9_.-]+/?$", u, re.I):
        return u

    # не youtube - возврат как есть
    if not (("youtube.com" in u) or ("youtu.be" in u)):
        return u

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(u, headers=headers, timeout=10, allow_redirects=True)
        final_url = force_https(resp.url or u)
        html = resp.text or ""

        # @handle в финальном URL
        m_final = re.search(
            r"https://(www\.)?youtube\.com/(@[A-Za-z0-9_.-]+)", final_url, re.I
        )
        if m_final:
            return f"https://www.youtube.com/{m_final.group(2)}"

        # rel=canonical -> @handle
        m_canon = re.search(
            r'rel=["\']canonical["\']\s+href=["\'](https?://(www\.)?youtube\.com/(@[A-Za-z0-9_.-]+))',
            html,
            re.I,
        )
        if m_canon:
            return force_https(m_canon.group(1))

        # canonicalBaseUrl в скриптах
        m_base = re.search(r'"canonicalBaseUrl":"\\?/(@[A-Za-z0-9_.-]+)"', html)
        if m_base:
            return f"https://www.youtube.com/{m_base.group(1)}"

        # og:url -> @handle
        m_og = re.search(
            r'property=["\']og:url["\']\s+content=["\'](https?://(www\.)?youtube\.com/(@[A-Za-z0-9_.-]+))',
            html,
            re.I,
        )
        if m_og:
            return force_https(m_og.group(1))

        # спец-кейс для /channel/ID
        if re.match(
            r"^https://(www\.)?youtube\.com/channel/[A-Za-z0-9_\-]+", final_url, re.I
        ):
            return final_url

        return final_url
    except Exception as e:
        logger.warning("youtube_to_handle error for %s: %s", u, e)
        return u


# Конверт watch/shorts/youtu.be в embed
def youtube_watch_to_embed(url: str) -> str:
    u = force_https(url or "")

    # youtu.be/<ID>
    m = re.search(r"https?://(?:www\.)?youtu\.be/([A-Za-z0-9_-]{11})", u, re.I)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"

    # youtube.com/watch?v=<ID>
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"

    # youtube.com/shorts/<ID>
    m = re.search(
        r"https?://(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})", u, re.I
    )
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?feature=oembed"

    return ""


# Получение заголовка видео через oEmbed API (fallback — og:title из HTML)
def youtube_oembed_title(url: str) -> str:
    o = ""
    try:
        oembed = (
            f"https://www.youtube.com/oembed?url={urlquote(url, safe='')}&format=json"
        )
        r = requests.get(oembed, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            o = (r.json() or {}).get("title", "") or ""
    except Exception:
        pass
    if o:
        return o

    # fallback: og:title
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        m = re.search(
            r'property=["\']og:title["\']\s+content=["\']([^"\']+)', r.text or "", re.I
        )
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


# ИзИзвлечение featured-видео с главной страницы канала (@handle / /channel/..)
def extract_youtube_featured_videos(channel_handle_url: str) -> list[dict]:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        html = requests.get(channel_handle_url, headers=headers, timeout=10).text

        # ytInitialData = {...};
        m = re.search(r"ytInitialData\s*=\s*(\{.*?\});", html, re.DOTALL)
        if not m:
            logger.warning("ytInitialData не найден на %s", channel_handle_url)
            return []

        data = json.loads(m.group(1))
        featured: list[dict] = []

        # поиск channelVideoPlayerRenderer в выбранной вкладке
        try:
            tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
            for tab in tabs:
                tabRenderer = tab.get("tabRenderer")
                if not tabRenderer or not tabRenderer.get("selected"):
                    continue
                content = tabRenderer.get("content", {})
                sectionList = content.get("sectionListRenderer", {}).get("contents", [])
                for section in sectionList:
                    items = section.get("itemSectionRenderer", {}).get("contents", [])
                    for item in items:
                        player = item.get("channelVideoPlayerRenderer")
                        if player:
                            video_id = player.get("videoId")
                            title_obj = player.get("title", {})
                            if "runs" in title_obj and title_obj["runs"]:
                                title = title_obj["runs"][0]["text"]
                            else:
                                title = title_obj.get("simpleText", "")
                            if video_id:
                                featured.append(
                                    {
                                        "videoId": video_id,
                                        "title": title,
                                        "url": f"https://www.youtube.com/watch?v={video_id}",
                                    }
                                )
        except Exception as e:
            logger.warning("Ошибка парсинга JSON YouTube: %s", e)
            return []

        return featured
    except Exception as e:
        logger.warning("Ошибка запроса/parsing YouTube: %s", e)
        return []
