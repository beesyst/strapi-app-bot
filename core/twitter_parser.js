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
        await page.goto(
        url.endsWith('/photo') ? url : (url.replace(/^https:\/\/twitter\.com/i,'https://x.com').replace(/\/+$/,'') + '/photo'),
        { timeout: 45000, waitUntil: 'domcontentloaded' }
        );

        // Ожидание появления аватара в любом из вариантов
        await page.waitForSelector(
        'img[src*="pbs.twimg.com/profile_images/"],' +
        'div[style*="pbs.twimg.com/profile_images/"],' +
        'meta[property="og:image"][content*="pbs.twimg.com/profile_images/"]',
        { timeout: 12000 }
        ).catch(() => { /* не критично - попытка вытащить без ожидания */ });

        // Небольшая задержка, чтобы дом дорисовался
        await page.waitForTimeout(1000);

        const result = await page.evaluate(() => {
        let links = [];
        let name = "";

        // display name
        const nameEl =
            document.querySelector('[data-testid="UserName"] span') ||
            document.querySelector('h2[role="heading"] > div > span');
        if (nameEl && nameEl.textContent) name = nameEl.textContent.trim();

        if (!name) {
            const t = (document.title || "").trim();
            const m = t.match(/^(.+?)\s*\(/) || t.match(/^(.+?)\s*\/\s/);
            name = (m && m[1]) ? m[1].trim() : t;
        }

        // BIO‑ссылки
        const bio = document.querySelector('[data-testid="UserDescription"]');
        if (bio) {
            const urls = bio.innerHTML.match(/https?:\/\/[^\s"<]+/g);
            if (urls) links = links.concat(urls);
            const naked = bio.textContent.match(/([a-zA-Z0-9-]+\.[a-zA-Z]{2,}\/[^\s]+)/g);
            if (naked) {
            naked.forEach(u => { if (!links.find(l => l.includes(u))) links.push('https://' + u); });
            }
        }

        // Ссылки под профилем
        document.querySelectorAll('[data-testid="UserProfileHeader_Items"] a, a[role="link"]').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (!href) return;
            if (href.startsWith('http') && !/x\.com|twitter\.com/.test(href)) links.push(href);
            if (href.startsWith('https://t.co/')) {
            const span = a.querySelector('span');
            const text = (span?.textContent || a.textContent || "").trim();
            if (/^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\/[^\s]+$/.test(text)) links.push('https://' + text);
            }
        });
        links = Array.from(new Set(links));

        // Ава: 3 источника
        let avatar = '';

        // <img src=".../profile_images/...">
        const img = Array.from(document.querySelectorAll('img'))
            .find(i => (i.src || '').includes('pbs.twimg.com/profile_images/'));
        if (img?.src) avatar = img.src;

        // <div style="background-image: url('.../profile_images/...')">
        if (!avatar) {
            const divBG = Array.from(document.querySelectorAll('div[style*="background-image"]'))
            .map(d => d.getAttribute('style') || '')
            .find(s => /pbs\.twimg\.com\/profile_images\//.test(s));
            if (divBG) {
            const m = divBG.match(/url\(["']?(https?:\/\/[^"')]+profile_images[^"')]+)["']?\)/i);
            if (m && m[1]) avatar = m[1];
            }
        }

        // <meta property="og:image" content=".../profile_images/...">
        if (!avatar) {
            const meta = document.querySelector('meta[property="og:image"]');
            const og = meta?.getAttribute('content') || '';
            if (/pbs\.twimg\.com\/profile_images\//.test(og)) avatar = og;
        }

        // Нормализация протокола и чистка HTML‑entities
        if (avatar) {
            avatar = avatar.replace(/^\/\//,'https://').replace(/&amp;/g,'&');
        }

        return { links, avatar, name };
        });



        // Вывод результата в stdout
        console.log(JSON.stringify(result));
    } catch (err) {
        console.error("Error:", err);
        process.exit(2);
    } finally {
        await browser.close();
    }
}

main();
