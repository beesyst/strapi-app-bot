import re
from urllib.parse import urljoin, urlparse

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


# Приведем все ссылки к списку [(a.text, href)]
def find_best_docs_link(soup, base_url):
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.text or "").strip().lower()
        href_full = urljoin(base_url, href)
        if any(
            keyword in text
            for keyword in ["docs", "documentation", "developer docs", "developers"]
        ):
            candidates.append((text, href_full))

    # Фильтруем только "docs", но без "api-docs" или "developer-docs"
    filtered = [
        (text, href)
        for text, href in candidates
        if not any(
            skip in href
            for skip in [
                "api-docs",
                "developer-docs",
                "apidocs",
                "api/",
                "api.",
                "developers",
            ]
        )
    ]

    # Теперь приоритет: domain.com/docs, docs.domain.com, остальное
    def score(href):
        parsed = urlparse(href)
        # domain.com/docs
        if re.match(r".*/docs/?$", parsed.path) and not parsed.netloc.startswith(
            "api."
        ):
            return 0
        # docs.domain.com
        if parsed.netloc.startswith("docs."):
            return 1
        # все остальные "docs" ссылки
        return 2

    if filtered:
        filtered.sort(key=lambda t: score(t[1]))
        return filtered[0][1]

    # fallback: ищем хоть что-то типа docs.domain.com или /docs
    all_hrefs = [urljoin(base_url, a["href"]) for a in soup.find_all("a", href=True)]
    for href in all_hrefs:
        parsed = urlparse(href)
        if re.match(r".*/docs/?$", parsed.path) or parsed.netloc.startswith("docs."):
            return href

    # если вообще ничего - вернем ""
    return ""


def extract_social_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = {k: "" for k in SOCIAL_PATTERNS if k != "documentURL"}

    # Собираем все соц-ссылки
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for key, pattern in SOCIAL_PATTERNS.items():
            if key == "documentURL":
                continue
            if pattern.search(href):
                links[key] = href

    links["websiteURL"] = base_url

    # Ищем док специальным методом
    document_url = find_best_docs_link(soup, base_url)
    if document_url:
        links["documentURL"] = document_url
    else:
        links["documentURL"] = ""

    return links
