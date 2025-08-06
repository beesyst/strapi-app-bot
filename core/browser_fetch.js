// core/browser_fetch.js
const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

const url = process.argv[2];
console.error('>>> browser_fetch.js запущен для: ' + url);  // <--- теперь ОК

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
    } catch (e) { /* бывает, идем дальше */ }

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
                    links[key] = href;
                }
            }
        });
        return links;
    });

    socials.websiteURL = url;

    console.log(JSON.stringify(socials));
    await browser.close();
})();
