const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

// ПОдключение fingerprint-generator (если есть в зависимостях)
let FingerprintGenerator = null;
try {
  const fg = require('fingerprint-generator');
  FingerprintGenerator = fg.FingerprintGenerator || fg;
} catch (e) {
  // не критично - будет использоваться только fingerprint-injector
}

// Разрешенные режимы ожидания навигации
const WAIT_STATES = new Set(['load', 'domcontentloaded', 'networkidle', 'commit', 'nowait']);

// Базовый User-Agent по умолчанию
const DEFAULT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
  'AppleWebKit(537.36) (KHTML, like Gecko) ' +
  'Chrome/124.0.0.0 Safari/537.36';

// Паттерны для детектирования антибот-страниц (Cloudflare и пр.)
const ANTI_BOT_PATTERNS = [
  'verifying you are human',
  'checking your browser',
  'review the security of your connection',
  'cf-challenge',
  'cloudflare',
  'attention required!',
];

// Разбор аргументов командной строки
function parseArgs(argv) {
  const args = {};
  let positionalUrl = null;

  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];

    if (a === '--html') args.html = true;
    else if (a === '--text') args.text = true;
    else if (a === '--raw') { args.html = true; args.text = true; args.raw = true; }
    else if (a === '--js') { args.js = String(argv[++i]).toLowerCase() !== 'false'; }
    else if (a === '--url') args.url = argv[++i];
    else if (a === '--wait') args.wait = argv[++i];
    else if (a === '--timeout') args.timeout = Number(argv[++i]);
    else if (a === '--ua') args.ua = argv[++i];
    else if (a === '--screenshot') args.screenshot = argv[++i];
    else if (a === '--headers') {
      try { args.headers = JSON.parse(argv[++i]); } catch { args.headers = {}; }
    } else if (a === '--cookies') {
      try { args.cookies = JSON.parse(argv[++i]); } catch { args.cookies = []; }
    } else if (a === '--retries') {
      args.retries = Math.max(0, Number(argv[++i]) || 0);
    } else if (a === '--captureNet') {
      args.captureNet = String(argv[++i] || 'true').toLowerCase() !== 'false';
    } else if (a === '--waitSocialHosts') {
      const csv = String(argv[++i] || '').trim();
      args.waitSocialHosts = csv
        ? csv.split(',').map(s => s.trim().toLowerCase()).filter(Boolean)
        : [];
    } else if (a === '--proxy') {
      try { args.proxy = JSON.parse(argv[++i]); } catch { args.proxy = {}; }
    } else if (a === '--profile') {
      args.profile = argv[++i];
    } else if (a === '--cookiesPath') {
      args.cookiesPath = argv[++i];
    } else if (a === '--scrollPages') {
      args.scrollPages = Math.max(1, Number(argv[++i]) || 1);
    }
    // fingerprint options
    else if (a === '--fp-device') args.fpDevice = argv[++i];
    else if (a === '--fp-os') args.fpOS = argv[++i];
    else if (a === '--fp-locales') args.fpLocales = argv[++i];
    else if (a === '--fp-viewport') args.fpViewport = argv[++i];
    // первый позиционный аргумент как URL
    else if (!a.startsWith('-') && !positionalUrl) {
      positionalUrl = a;
    }
  }

  if (!args.url && positionalUrl) args.url = positionalUrl;
  return args;
}

// Простая эвристика антибот-страниц (Cloudflare и подобные)
async function detectAntiBot(page, response) {
  try {
    const server = response?.headers()?.server || '';
    const status = typeof response?.status === 'function' ? response.status() : null;

    if (status === 403 || status === 503) {
      return { detected: true, kind: status === 403 ? '403' : '503', server };
    }

    if (server && /cloudflare/i.test(server)) {
      const html = await page.content();
      const low = (html || '').slice(0, 50000).toLowerCase();
      const hit = ANTI_BOT_PATTERNS.find(p => low.includes(p));
      if (hit) return { detected: true, kind: 'cloudflare', server };
    }

    const hasCF = await page.evaluate(() => {
      const text = (document.body?.innerText || '').toLowerCase();
      const selectors = [
        '#cf-challenge-running',
        'div#cf-please-wait',
        'div.cf-browser-verification',
        'div[id*="challenge"]',
      ];
      if (selectors.some(sel => document.querySelector(sel))) return true;
      return /verifying you are human|checking your browser|review the security|cloudflare|attention required!/i.test(text);
    });

    if (hasCF) {
      return { detected: true, kind: 'cloudflare', server };
    }

    return { detected: false, kind: '', server };
  } catch (e) {
    console.error('detectAntiBot failed:', e?.message || e);
    return { detected: false, kind: '', server: '' };
  }
}

// Создание контекста браузера с отпечатком (fingerprint-injector / fingerprint-generator)
async function buildContextWithFingerprint(browser, {
  targetUrl,
  ua,
  js,
  headers,
  fpDevice,
  fpOS,
  fpLocales,
  fpViewport,
}) {
  let devices = undefined;
  if (fpDevice) {
    const d = String(fpDevice).toLowerCase();
    if (['desktop', 'mobile', 'tablet'].includes(d)) devices = [d];
  }

  let operatingSystems = undefined;
  if (fpOS) {
    const os = String(fpOS).toLowerCase();
    if (['windows', 'linux', 'macos', 'ios', 'android'].includes(os)) operatingSystems = [os];
  }

  let locales = undefined;
  if (fpLocales) {
    const arr = String(fpLocales).split(',').map(s => s.trim()).filter(Boolean);
    if (arr.length) locales = arr;
  }

  let viewport = undefined;
  if (fpViewport) {
    const m = String(fpViewport).match(/^(\d+)\s*x\s*(\d+)$/i);
    if (m) viewport = { width: Number(m[1]), height: Number(m[2]) };
  }

  // если есть fingerprint-generator - генерим отпечаток явно
  if (FingerprintGenerator && (devices || operatingSystems || locales || viewport || (ua && String(ua).trim()))) {
    try {
      const fg = new FingerprintGenerator({
        browsers: [{ name: 'chrome' }],
        devices: devices || ['desktop'],
        operatingSystems: operatingSystems || ['windows', 'linux'],
        locales,
      });

      const { fingerprint } = fg.getFingerprint({ url: targetUrl });

      const finalViewport = viewport || fingerprint.viewport || { width: 1366, height: 768 };
      const finalLocale = (locales && locales[0]) || (fingerprint.languages && fingerprint.languages[0]) || 'en-US';

      const newContextOptions = {
        ...(ua && String(ua).trim() ? { userAgent: ua } : {}),
        viewport: finalViewport,
        locale: finalLocale,
        javaScriptEnabled: js !== false,
        ignoreHTTPSErrors: true,
        bypassCSP: true,
        extraHTTPHeaders: { ...headers, 'Accept-Language': finalLocale + ',en;q=0.9' },
      };

      return await newInjectedContext(browser, { fingerprint, newContextOptions });
    } catch (e) {
      console.error('buildContextWithFingerprint (fingerprint-generator) failed:', e?.message || e);
    }
  }

  // fingerprint-injector сам генерирует отпечаток
  try {
    const baseOptions = {
      ...(ua && String(ua).trim() ? { userAgent: ua } : { userAgent: DEFAULT_UA }),
      javaScriptEnabled: js !== false,
      ignoreHTTPSErrors: true,
      bypassCSP: true,
      viewport: { width: 1366, height: 768 },
      extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
    };
    return await newInjectedContext(browser, { newContextOptions: baseOptions });
  } catch (e) {
    console.error('fingerprint-injector failed, fallback to plain context:', e?.message || e);
    // обычный Playwright-контекст
    return await browser.newContext({
      userAgent: ua || DEFAULT_UA,
      javaScriptEnabled: js !== false,
      ignoreHTTPSErrors: true,
      bypassCSP: true,
      viewport: { width: 1366, height: 768 },
      extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
    });
  }
}

// Надежная навигация с несколькими режимами ожидания
async function robustGoto(page, targetUrl, waitUntil, timeout) {
  const perTry = Math.min(timeout, 20000);
  const primary = (waitUntil === 'networkidle') ? 'domcontentloaded' : (waitUntil || 'domcontentloaded');

  const tries = [
    { waitUntil: primary,       timeout: perTry },
    { waitUntil: 'load',        timeout: perTry },
    { waitUntil: 'commit',      timeout: perTry },
    { waitUntil: 'networkidle', timeout: Math.min(timeout, 15000) },
  ];

  for (const opt of tries) {
    try {
      const r = await page.goto(targetUrl, opt);
      try { await page.waitForLoadState('domcontentloaded', { timeout: 5000 }); } catch {}
      try { await page.waitForLoadState('networkidle',      { timeout: 5000 }); } catch {}
      return r;
    } catch (e) {
      console.error('goto failed with', opt.waitUntil, e?.message || e);
    }
  }
  return null;
}

// Ожидание появления ссылок с указанными хостами (для ленивых виджетов)
async function waitForAnySocialHost(page, hosts, ms) {
  if (!Array.isArray(hosts) || hosts.length === 0) return;
  try {
    await page.waitForFunction((arr) => {
      const H = (arr || []).map(s => String(s || '').toLowerCase());
      const as = Array.from(document.querySelectorAll('a[href]'));
      for (const a of as) {
        const href = String(a.getAttribute('href') || '').toLowerCase();
        if (!href) continue;
        if (H.some(h => href.includes(h))) return true;
        try {
          const abs = new URL(href, location.href).href.toLowerCase();
          if (H.some(h => abs.includes(h))) return true;
        } catch {}
      }
      return false;
    }, { timeout: ms }, hosts);
  } catch {}
}

// Простой скролл по странице для загрузки ленивого контента
async function scrollPage(page, pagesCount) {
  if (!pagesCount || pagesCount <= 0) return;
  try {
    await page.evaluate(async (n) => {
      const delay = (ms) => new Promise(r => setTimeout(r, ms));
      for (let i = 0; i < n; i++) {
        window.scrollTo(0, document.body.scrollHeight);
        await delay(250);
      }
    }, pagesCount);
  } catch {}
}

// Основная функция браузерного фетча
async function browserFetch(opts) {
  const {
    url,
    wait = 'domcontentloaded',
    timeout = 30000,
    ua,
    headers = {},
    cookies = [],
    screenshot,
    html = false,
    text = false,
    js = true,
    retries = 1,
    fpDevice,
    fpOS,
    fpLocales,
    fpViewport,
    captureNet = false,
    waitSocialHosts = [],
    cookiesPath,
    scrollPages = 1,
    proxy,
    profile, // пока не используем persistent-профили, но флаг оставлен для совместимости
  } = opts || {};

  if (!url) throw new Error('url is required');

  const waitUntil = WAIT_STATES.has(wait) ? (wait === 'nowait' ? null : wait) : 'domcontentloaded';

  const launchArgs = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-blink-features=AutomationControlled',
  ];

  const consoleLogs = [];
  const netlog = [];

  const attempt = async () => {
    const startedAt = Date.now();
    const launchOpts = { headless: true, args: launchArgs };

    if (proxy && Object.keys(proxy).length) {
      launchOpts.proxy = proxy;
    }

    let browser = null;
    let context = null;

    try {
      browser = await chromium.launch(launchOpts);

      context = await buildContextWithFingerprint(browser, {
        targetUrl: url,
        ua,
        js,
        headers,
        fpDevice,
        fpOS,
        fpLocales,
        fpViewport,
      });

      if (Array.isArray(cookies) && cookies.length) {
        try { await context.addCookies(cookies); } catch {}
      }

      const page = await context.newPage();

      // перехватываем window.open, чтобы видеть, куда страница пытается уйти при кликах
      await page.addInitScript(() => {
        try {
          // буфер для пойманных URL
          window.__SAB_OPENED_URLS = [];

          const originalOpen = window.open;
          window.open = function (...args) {
            const url = args[0];
            if (typeof url === 'string') {
              try {
                window.__SAB_OPENED_URLS.push(url);
              } catch {}
            }
            if (typeof originalOpen === 'function') {
              return originalOpen.apply(this, args);
            }
            return null;
          };
        } catch (e) {
          // не ломаем страницу, если что-то пошло не так
          console.error('window.open hook failed:', e && (e.message || e));
        }
      });

      page.on('console', (msg) => {
        try { consoleLogs.push({ type: msg.type(), text: msg.text() }); } catch {}
      });

      if (captureNet) {
        page.on('request', (req) => {
          try {
            netlog.push({
              dir: 'req',
              url: req.url() || '',
              method: req.method(),
              headers: req.headers() || {},
              postData: req.postData() || null,
              resourceType: req.resourceType(),
            });
          } catch {}
        });
        page.on('response', async (res) => {
          try {
            const u = res.url() || '';
            const hs = res.headers() || {};
            const st = (typeof res.status === 'function') ? res.status() : 0;
            netlog.push({
              dir: 'res',
              url: u,
              status: st,
              headers: hs,
            });
          } catch {}
        });
      }

      const resp = await robustGoto(page, url, waitUntil, timeout);

      // легкое ожидание появления ссылок (по хостам) - чисто навигация, без логики соцсетей
      await waitForAnySocialHost(page, waitSocialHosts, 7000);

      // скролл для ленивого контента
      await scrollPage(page, scrollPages);

      // пробуем "достучаться" до соц-иконок/кнопок без href:
      try {
        await page.evaluate(async () => {
          const delay = (ms) => new Promise((r) => setTimeout(r, ms));

          const imgTokens = [
            'discord',
            'twitter',
            'x-',
            'telegram',
            't.me',
            'github',
            'linkedin',
            'youtube',
            'medium',
            'reddit',
          ];

          const textTokens = [
            'twitter',
            'x (twitter)',
            'x, formerly twitter',
            'discord',
            'telegram',
            'github',
            'youtube',
            'medium',
            'reddit',
          ];

          const isSocialImg = (img) => {
            const src = (img.getAttribute('src') || '').toLowerCase();
            const alt = (img.getAttribute('alt') || '').toLowerCase();
            return imgTokens.some((t) => src.includes(t) || alt.includes(t));
          };

          const clickables = new Set();

          // старый вариант - картинки-иконки
          const imgs = Array.from(document.querySelectorAll('img')).filter(isSocialImg);
          for (const img of imgs) {
            const btn =
              img.closest('a, button, [role="button"], [tabindex]') ||
              img;
            if (btn) clickables.add(btn);
          }

          // кнопки/линки с текстом "Twitter", "Discord" и т.п.
          const nodesWithText = Array.from(
            document.querySelectorAll('a, button, [role="button"], [tabindex]')
          );
          for (const el of nodesWithText) {
            const txt = (el.innerText || el.textContent || '').toLowerCase().trim();
            if (!txt) continue;
            if (textTokens.some((t) => txt.includes(t))) {
              clickables.add(el);
            }
          }

          // кликаем все, что насобирали
          for (const el of clickables) {
            try {
              el.dispatchEvent(
                new MouseEvent('click', { bubbles: true, cancelable: true })
              );
            } catch {}
            await delay(400);
          }
        });
      } catch (e) {
        console.error('social click helper failed:', e && (e.message || e));
      }

      if (screenshot) {
        try { await page.screenshot({ path: screenshot, fullPage: true }); } catch {}
      }

      const finalUrl = page.url();

      let status = 0;
      try { status = resp ? (typeof resp.status === 'function' ? resp.status() : 0) : 0; } catch {}

      let bodyHtml = null;
      let bodyText = null;
      if (html || opts.raw) {
        try { bodyHtml = await page.content(); } catch {}
      }
      if (text || (!html && !opts.raw)) {
        try { bodyText = await page.evaluate(() => document.body?.innerText || ''); } catch {}
      }

      const title = await page.title().catch(() => '');

      const headersObj = {};
      if (resp) {
        try {
          Object.entries(resp.headers() || {}).forEach(([k, v]) => {
            headersObj[k] = v;
          });
        } catch {}
      }

      const cookiesOut = await context.cookies().catch(() => []);
      if (Array.isArray(cookiesOut) && cookiesPath) {
        try {
          const fs = require('fs');
          const path = require('path');
          fs.mkdirSync(path.dirname(cookiesPath), { recursive: true });
          fs.writeFileSync(cookiesPath, JSON.stringify(cookiesOut, null, 2));
        } catch {}
      }

      const antiBot = await detectAntiBot(page, resp);
      const timing = { startedAt, finishedAt: Date.now(), ms: Date.now() - startedAt };

      // вытаскиваем все URL, которые страница попыталась открыть (через window.open)
      let openedUrls = [];
      try {
        const opened = await page.evaluate(() =>
          Array.isArray(window.__SAB_OPENED_URLS) ? window.__SAB_OPENED_URLS : []
        );

        openedUrls = Array.from(
          new Set(
            (opened || []).filter(
              (u) => typeof u === 'string' && /^https?:\/\//i.test(u)
            )
          )
        );
      } catch (e) {
        console.error('openedUrls extract failed:', e && (e.message || e));
      }

      const result = {
        ok: true,
        status,
        url,
        finalUrl,
        title,
        html: bodyHtml,
        text: bodyText,
        headers: headersObj,
        cookies: cookiesOut,
        console: consoleLogs,
        timing,
        antiBot,
        website: url,
      };

      if (captureNet) {
        result.netlog = netlog;
      }

      // просто пробрасываем все URL, открытые через window.open
      if (openedUrls.length) {
        result.openedUrls = openedUrls;
      }

      return result;

    } finally {
      try {
        if (browser) {
          await browser.close();
        } else if (context) {
          await context.close();
        }
      } catch {}
    }
  };

  let lastError = null;
  for (let i = 0; i < Math.max(1, retries); i++) {
    try {
      const res = await attempt();
      return res;
    } catch (e) {
      lastError = e;
    }
  }

  return {
    ok: false,
    status: 0,
    url,
    finalUrl: url,
    title: '',
    html: null,
    text: null,
    headers: {},
    cookies: [],
    console: consoleLogs,
    timing: { error: String(lastError && (lastError.message || lastError)) },
    antiBot: { detected: false, kind: '', server: '' },
    website: url,
  };
}

// Точка входа при запуске файла как CLI-скрипта
async function main() {
  if (require.main !== module) return;

  const args = parseArgs(process.argv);

  if (!args.url) {
    process.stdout.write(JSON.stringify({
      ok: false,
      status: 0,
      url: null,
      error: 'url is required',
    }));
    process.exitCode = 1;
    return;
  }

  try {
    const result = await browserFetch(args);

    // специальный компактный формат для --raw (совместимость с twitter.py / Nitter)
    if (args.raw) {
      const instance = (() => {
        try {
          const u = result.finalUrl || result.url || args.url;
          return new URL(u).origin;
        } catch {
          return '';
        }
      })();

      const out = {
        ok: !!(result && result.ok !== false),
        html: result.html || '',
        status: result.status || 0,
        antiBot: result.antiBot || { detected: false, kind: '', server: '' },
        instance,
      };

      process.stdout.write(JSON.stringify(out));
    } else {
      process.stdout.write(JSON.stringify(result, null, 2));
    }
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      status: 0,
      url: args.url || null,
      error: String(e && (e.message || e)),
    }));
    process.exitCode = 1;
  }
}

module.exports = { browserFetch, parseArgs };

// Автозапуск main при прямом вызове
main();