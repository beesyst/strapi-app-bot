// core/browser_fetch.js
const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

const url = process.argv[2];
console.error('>>> browser_fetch.js запущен для: ' + url);

(async () => {
    if (!url) {
        console.error("No URL provided");
        process.exit(1);
    }
    const browser = await chromium.launch({ headless: true });
    const context = await newInjectedContext(browser, {});
    const page = await context.newPage();
    await page.goto(url, { timeout: 45000, waitUntil: 'domcontentloaded' });

    try {
        await page.waitForSelector(
            'a[href*="twitter.com"],a[href*="discord.gg"],a[href*="t.me"],a[href*="github.com"],a[href*="linkedin.com"],a[href*="youtube.com"],a[href*="medium.com"]',
            { timeout: 12000 }
        );
    } catch (e) { /* для React/Vue/SPA */ }

    // Соцсети
    const socials = await page.evaluate(() => {
        const patterns = {
            twitterURL: /twitter\.com|x\.com/i,
            discordURL: /discord\.gg|discord\.com/i,
            telegramURL: /t\.me|telegram\.me/i,
            youtubeURL: /youtube\.com|youtu\.be/i,
            linkedinURL: /linkedin\.com/i,
            redditURL: /reddit\.com/i,
            mediumURL: /medium\.com/i,
            githubURL: /github\.com/i,
        };
        const links = {};
        Object.keys(patterns).forEach(key => { links[key] = ""; });
        document.querySelectorAll("a[href]").forEach(a => {
            const href = a.getAttribute("href");
            for (const [key, rx] of Object.entries(patterns)) {
                if (rx.test(href) && !links[key]) {
                    links[key] = href.startsWith("http") ? href : "https://www.youtube.com" + href;
                }
            }
        });
        return links;
    });

    socials.websiteURL = url;

    // Парс featured-видео только если это YouTube канал
    let featuredVideos = [];
    if (
        /^https:\/\/(www\.)?youtube\.com\/(@|channel\/)[^/]+/i.test(url)
    ) {
        try {
            featuredVideos = await page.evaluate(() => {
                let featured = [];
                // Через ytInitialData
                let data;
                try {
                    data = window.ytInitialData;
                } catch (e) {}
                if (!data) {
                    // Иногда ytInitialData лежит только в <script>
                    for (const s of Array.from(document.scripts)) {
                        if (s.textContent && s.textContent.includes('ytInitialData')) {
                            const match = s.textContent.match(/ytInitialData\s*=\s*(\{.*?\});/s);
                            if (match) {
                                try { data = JSON.parse(match[1]); } catch {}
                                break;
                            }
                        }
                    }
                }
                if (data) {
                    try {
                        const tabs = data.contents?.twoColumnBrowseResultsRenderer?.tabs || [];
                        for (const tab of tabs) {
                            if (!tab.tabRenderer || !tab.tabRenderer.selected) continue;
                            const sections = tab.tabRenderer.content?.sectionListRenderer?.contents || [];
                            for (const section of sections) {
                                const items = section.itemSectionRenderer?.contents || [];
                                for (const item of items) {
                                    const player = item.channelVideoPlayerRenderer;
                                    if (player && player.videoId) {
                                        featured.push({
                                            videoId: player.videoId,
                                            title: player.title?.runs?.[0]?.text || "",
                                            url: `https://www.youtube.com/watch?v=${player.videoId}`
                                        });
                                    }
                                }
                            }
                        }
                    } catch (e) {}
                }
                // Если ничего не нашли - fallback на DOM
                if (!featured.length) {
                    // Найти <a href="/watch?v=..."> в трейлере
                    const a = document.querySelector('ytd-channel-video-player-renderer a[href*="/watch"]');
                    const title = a?.textContent?.trim() || "";
                    const href = a?.getAttribute("href") || "";
                    if (href && href.startsWith("/watch?v=")) {
                        const videoId = href.split('v=')[1].split('&')[0];
                        featured.push({
                            videoId,
                            title,
                            url: "https://www.youtube.com" + href
                        });
                    }
                }
                return featured;
            });
        } catch (e) {
            console.error('Ошибка парсинга featured видео:', e);
            featuredVideos = [];
        }
    }


    if (featuredVideos && featuredVideos.length) {
        socials.featuredVideos = featuredVideos;
    }

    console.log(JSON.stringify(socials));
    await browser.close();
})();
