// core/twitter_parser.js
const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

async function main() {
    const url = process.argv[2];
    if (!url) {
        console.error("No Twitter/X URL provided");
        process.exit(1);
    }

    // Chromium с инжекцией отпечатка
    const browser = await chromium.launch({ headless: true });
    const context = await newInjectedContext(browser, {
        // fingerprintOptions и newContextOptions можно настроить при необходимости
        // fingerprintOptions: { devices: ['desktop'], operatingSystems: ['windows'] }
    });

    const page = await context.newPage();

    try {
        await page.goto(url, { timeout: 45000, waitUntil: 'domcontentloaded' });
        // Ждем динамику X
        await page.waitForTimeout(3500);

        const result = await page.evaluate(() => {
            let links = [];

            // BIO (https:// и "linktr.ee/xxx"-стайл)
            const bio = document.querySelector('[data-testid="UserDescription"]');
            if (bio) {
                const urls = bio.innerHTML.match(/https?:\/\/[^\s"<]+/g);
                if (urls) links = links.concat(urls);
                const nakedUrls = bio.textContent.match(/([a-zA-Z0-9-]+\.[a-zA-Z]{2,}\/[^\s]+)/g);
                if (nakedUrls) {
                    nakedUrls.forEach(u => {
                        if (!links.find(l => l.includes(u))) {
                            links.push('https://' + u);
                        }
                    });
                }
            }

            // Ссылки под профилем (обычно t.co, linktr.ee и т.п.)
            const linkBlock = document.querySelectorAll('[data-testid="UserProfileHeader_Items"] a, a[role="link"]');
            for (const a of linkBlock) {
                const href = a.getAttribute('href');
                if (!href) continue;
                // Ссылка сразу нормальная
                if (href.startsWith('http') && !href.includes('x.com') && !href.includes('twitter.com')) {
                    links.push(href);
                }
                // t.co редирект, а текст содержит видимую ссылку
                if (href.startsWith('https://t.co/')) {
                    const span = a.querySelector('span');
                    let naked = null;
                    if (span && span.textContent.match(/^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\/[^\s]+$/)) {
                        naked = span.textContent;
                    } else if (a.textContent && a.textContent.match(/^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\/[^\s]+$/)) {
                        naked = a.textContent.trim();
                    }
                    if (naked && !links.includes('https://' + naked)) {
                        links.push('https://' + naked);
                    }
                }
            }

            // Уникализируем ссылки
            links = Array.from(new Set(links));

            // Аватар
            let avatar = '';
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                if (img.src.includes('pbs.twimg.com/profile_images/')) {
                    avatar = img.src;
                    break;
                }
            }

            return { links, avatar };
        });

        // Выводим результат в stdout
        console.log(JSON.stringify(result));
    } catch (err) {
        console.error("Error:", err);
        process.exit(2);
    } finally {
        await browser.close();
    }
}

main();
