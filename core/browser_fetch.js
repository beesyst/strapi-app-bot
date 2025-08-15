// core/browser_fetch.js
const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

const url = process.argv[2];
console.error('>>> browser_fetch.js запущен для: ' + url);

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

async function makeContext(browser) {
  // попытка создать контекст с fingerprint-injector; при падении - безопасный fallback
  try {
    return await newInjectedContext(browser, {});
  } catch (e) {
    console.error('fingerprint-injector failed, fallback to plain context:', e?.message || e);
    return await browser.newContext({
      userAgent: UA,
      locale: 'en-US',
      javaScriptEnabled: true,
      ignoreHTTPSErrors: true,
      bypassCSP: true,
      viewport: { width: 1366, height: 768 },
      extraHTTPHeaders: { 'Accept-Language': 'en-US,en;q=0.9' },
    });
  }
}

async function robustGoto(page, targetUrl) {
  const tries = [
    { waitUntil: 'domcontentloaded', timeout: 45000 },
    { waitUntil: 'load',            timeout: 45000 },
    { waitUntil: 'commit',          timeout: 45000 },
  ];
  for (const opts of tries) {
    try {
      await page.goto(targetUrl, opts);
      try { await page.waitForLoadState('networkidle', { timeout: 12000 }); } catch {}
      return true;
    } catch (e) {
      console.error('goto failed with', opts.waitUntil, e?.message || e);
    }
  }
  return false;
}

function normalizeTwitter(u) {
  try {
    return u.replace(/https?:\/\/(www\.)?twitter\.com/i, 'https://x.com');
  } catch { return u; }
}

function absUrl(href, base) {
  try { return href.startsWith('http') ? href : new URL(href, base).href; }
  catch { return href; }
}

(async () => {
  if (!url) {
    console.error('No URL provided');
    console.log(JSON.stringify({ websiteURL: '', error: 'no_url' }));
    process.exit(0);
  }

  const browser = await chromium.launch({
    headless: true,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--no-sandbox',
      '--disable-gpu',
    ],
  });

  let context;
  try {
    context = await makeContext(browser);
    const page = await context.newPage();

    const ok = await robustGoto(page, url);
    if (!ok) {
      console.log(JSON.stringify({ websiteURL: url, error: 'goto_failed' }));
      await browser.close();
      process.exit(0);
    }

    // ленивая отрисовка футера/виджетов
    try {
      await page.evaluate(async () => {
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise(r => setTimeout(r, 600));
      });
    } catch {}

    // ожидание основных якорей соцсетей (включая x.com)
    try {
      await page.waitForSelector(
        [
          'a[href*="twitter.com"]',
          'a[href*="x.com"]',
          'a[href*="discord.gg"]',
          'a[href*="discord.com"]',
          'a[href*="t.me"]',
          'a[href*="telegram.me"]',
          'a[href*="github.com"]',
          'a[href*="linkedin.com"]',
          'a[href*="youtube.com"]',
          'a[href*="medium.com"]',
          'a[href*="reddit.com"]',
        ].join(','),
        { timeout: 12000 }
      );
    } catch {}

    // сбор соцссылок
    const socials = await page.evaluate((base) => {
      const rxTwitter = /twitter\.com|x\.com/i;
      const patterns = {
        twitterURL: rxTwitter,
        discordURL: /discord\.gg|discord\.com/i,
        telegramURL: /t\.me|telegram\.me/i,
        youtubeURL: /youtube\.com|youtu\.be/i,
        linkedinURL: /linkedin\.com/i,
        redditURL: /reddit\.com/i,
        mediumURL: /medium\.com/i,
        githubURL: /github\.com/i,
      };
      const toAbs = (href) => {
        try { return href.startsWith('http') ? href : new URL(href, base).href; }
        catch { return href; }
      };
      const links = Object.fromEntries(Object.keys(patterns).map(k => [k, '']));
      const twitterAll = new Set();

      document.querySelectorAll('a[href]').forEach(a => {
        const href = a.getAttribute('href') || '';
        for (const [key, rx] of Object.entries(patterns)) {
          if (!links[key] && rx.test(href)) {
            const abs = toAbs(href);
            links[key] = abs;
          }
        }
        if (rxTwitter.test(href)) {
          twitterAll.add(toAbs(href));
        }
      });

      // возврат массива всех "твиттеров" на странице
      return { ...links, twitterAll: Array.from(twitterAll) };
    }, url);

    // нормализация twitter -> x.com
    if (socials.twitterURL) socials.twitterURL = normalizeTwitter(socials.twitterURL);
    if (Array.isArray(socials.twitterAll)) {
      socials.twitterAll = socials.twitterAll.map(normalizeTwitter);
    }


    // нормализация twitter -> x.com
    if (socials.twitterURL) socials.twitterURL = normalizeTwitter(socials.twitterURL);

    socials.websiteURL = url;

    // YouTube featured
    let featuredVideos = [];
    if (/^https:\/\/(www\.)?youtube\.com\/(@|channel\/)[^/]+/i.test(url)) {
      try {
        featuredVideos = await page.evaluate(() => {
          let featured = [];
          let data = null;
          try { data = window.ytInitialData; } catch {}
          if (!data) {
            for (const s of Array.from(document.scripts)) {
              if (s.textContent && s.textContent.includes('ytInitialData')) {
                const m = s.textContent.match(/ytInitialData\s*=\s*(\{.*?\});/s);
                if (m) { try { data = JSON.parse(m[1]); } catch {} break; }
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
                    if (player?.videoId) {
                      featured.push({
                        videoId: player.videoId,
                        title: player.title?.runs?.[0]?.text || '',
                        url: `https://www.youtube.com/watch?v=${player.videoId}`,
                      });
                    }
                  }
                }
              }
            } catch {}
          }
          if (!featured.length) {
            const a = document.querySelector('ytd-channel-video-player-renderer a[href*="/watch"]');
            const title = a?.textContent?.trim() || '';
            const href = a?.getAttribute('href') || '';
            if (href && href.startsWith('/watch?v=')) {
              const videoId = href.split('v=')[1].split('&')[0];
              featured.push({ videoId, title, url: 'https://www.youtube.com' + href });
            }
          }
          return featured;
        });
      } catch (e) {
        console.error('Ошибка парсинга featured видео:', e?.message || e);
      }
    }
    if (featuredVideos && featuredVideos.length) {
      socials.featuredVideos = featuredVideos;
    }

    // гарантированный JSON
    try {
      console.log(JSON.stringify(socials));
    } catch {
      console.log(JSON.stringify({ websiteURL: url }));
    }

    await browser.close();
    process.exit(0);
  } catch (e) {
    console.error('Fatal error:', e?.message || e);
    try { console.log(JSON.stringify({ websiteURL: url, error: 'fatal' })); } catch {}
    try { await browser.close(); } catch {}
    process.exit(0);
  }
})();
