import re

from bs4 import BeautifulSoup

SOCIAL_PATTERNS = {
    "twitterURL": re.compile(r"twitter\.com|x\.com", re.I),
    "discordURL": re.compile(r"discord\.gg|discord\.com", re.I),
    "telegramURL": re.compile(r"t\.me|telegram\.me", re.I),
    "youtubeURL": re.compile(r"youtube\.com|youtu\.be", re.I),
    "linkedinURL": re.compile(r"linkedin\.com", re.I),
    "redditURL": re.compile(r"reddit\.com", re.I),
    "mediumURL": re.compile(r"medium\.com", re.I),
    "githubURL": re.compile(r"github\.com", re.I),
    "websiteURL": re.compile(
        r"^https?://(?!twitter|x|discord|t\.me|youtube|linkedin|reddit|medium|github)",
        re.I,
    ),
    "documentURL": re.compile(r"docs\.", re.I),
}


def extract_social_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for key, pattern in SOCIAL_PATTERNS.items():
            if pattern.search(href):
                # Для docs берем только главную страницу, если ссылок много — первую
                if key == "documentURL":
                    if not links[key]:
                        links[key] = href
                else:
                    links[key] = href
    links["websiteURL"] = base_url  # Всегда проставляем
    return links
