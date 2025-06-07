// core/twitter_parser.js
const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

async function main() {
    const url = process.argv[2];
    if (!url) {
        console.error("No Twitter URL provided");
        process.exit(1);
    }
    const browser = await chromium.launch({ headless: true });
    const context = await newInjectedContext(browser, {});
    const page = await context.newPage();
    await page.goto(url, { timeout: 45000, waitUntil: 'domcontentloaded' });

    // Даем время подгрузиться динамике X
    await page.waitForTimeout(3500);

    const result = await page.evaluate(() => {
        let links = [];

        // 1. Ссылки из BIO (https:// и "linktr.ee/xxx"-стайл)
        const bio = document.querySelector('[data-testid="UserDescription"]');
        if (bio) {
            // Все https ссылки как раньше
            const urls = bio.innerHTML.match(/https?:\/\/[^\s"<]+/g);
            if (urls) links = links.concat(urls);
            // Плюс: если просто textContent похож на что-то типа "linktr.ee/altlayer"
            const nakedUrls = bio.textContent.match(/([a-zA-Z0-9-]+\.[a-zA-Z]{2,}\/[^\s]+)/g);
            if (nakedUrls) {
                nakedUrls.forEach(u => {
                    // Не добавляем, если уже есть как https
                    if (!links.find(l => l.includes(u))) {
                        links.push('https://' + u);
                    }
                });
            }
        }

        // Ссылки под профилем (обычно t.co, но видно linktr.ee и т.п.)
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
                // Если внутри есть span — берем textContent у span
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

        // Лого 
        let avatar = '';
        const imgs = Array.from(document.querySelectorAll('img'));
        for (const img of imgs) {
            // Ищем ссылку на profile_images
            if (img.src.includes('pbs.twimg.com/profile_images/')) {
                avatar = img.src;
                break;
            }
        }

        return { links, avatar };
    });

    console.log(JSON.stringify(result));
    await browser.close();
}

main();
