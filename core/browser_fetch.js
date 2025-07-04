// core/browser_fetch.js
const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

(async () => {
    const url = process.argv[2];
    if (!url) {
        console.error("No URL provided");
        process.exit(1);
    }
    const browser = await chromium.launch({ headless: true });
    const context = await newInjectedContext(browser, {});
    const page = await context.newPage();
    await page.goto(url, { timeout: 45000, waitUntil: 'domcontentloaded' });
    // Можно waitForTimeout если нужно
    const html = await page.content();
    console.log(html);
    await browser.close();
})();
