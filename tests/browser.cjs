const assert = require('node:assert/strict');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require('playwright');

const servers = [];
async function server(auth = false) {
  const child = spawn(process.env.PYTHON || 'python3', ['tests/serve_fixture.py', ...(auth ? ['--auth'] : [])], {stdio: ['ignore', 'pipe', 'pipe']});
  servers.push(child);
  return new Promise((resolve, reject) => {
    let output = '', errors = '';
    const timer = setTimeout(() => reject(new Error('Fixture server startup timed out')), 20000);
    child.stderr.on('data', chunk => errors += chunk);
    child.on('error', reject);
    child.on('exit', code => {clearTimeout(timer); if (code) reject(new Error('Fixture server exited: ' + errors));});
    child.stdout.on('data', chunk => {
      output += chunk;
      if (output.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(output.split('\n')[0]));}
    });
  });
}

async function language(page, value) {
  await Promise.all([page.waitForEvent('load'), page.selectOption('#language-select', value)]);
  assert.equal(await page.locator('html').getAttribute('lang'), value);
}

async function noOverflow(page) {
  const overflow = await page.evaluate(() => ({width: innerWidth, actual: document.documentElement.scrollWidth, elements: [...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => el.tagName + '.' + el.className).slice(0, 10)}));
  assert(overflow.actual <= overflow.width + 1, 'Page overflows viewport: ' + JSON.stringify(overflow));
}

(async () => {
  let browser;
  try {
    fs.mkdirSync('artifacts', {recursive: true});
    browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined});
    const errors = [];
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    const plain = await server();
    await page.goto(plain.origin);
    await page.locator('[data-entry]').first().waitFor();
    assert.equal(await page.locator('[data-entry]').count(), 2);
    await page.screenshot({path: 'artifacts/desktop-zh.png', fullPage: true});
    await language(page, 'en');
    await page.locator('[data-entry]').first().waitFor();
    assert.match(await page.locator('#page-title').innerText(), /Cache workspace/);
    await noOverflow(page);
    await page.locator('[data-entry]').first().click();
    await page.locator('.source-document').waitFor();
    await page.waitForFunction(() => !document.querySelector('.source-document .loading-ring'));
    assert.match(await page.locator('#detail-content').innerText(), /Why this priority/);
    await page.screenshot({path: 'artifacts/detail-en.png'});
    await page.locator('#detail-dialog .close-dialog').click();
    await page.locator('#search').fill('this-will-not-match-any-entry');
    await page.locator('#reset-filters').waitFor();
    assert.match(await page.locator('#entries').innerText(), /No entries yet/);
    await page.locator('#reset-filters').click();
    await page.locator('[data-entry]').first().waitFor();
    await page.locator('#method-button').click();
    assert(!/[\u3400-\u9fff]/.test(await page.locator('#method-body').innerText()));
    await page.locator('#method-dialog .close-dialog').click();
    await page.screenshot({path: 'artifacts/desktop-en.png', fullPage: true});
    await page.reload();
    assert.equal(await page.locator('#language-select').inputValue(), 'en');
    for (const width of [390, 320, 768]) {
      await page.setViewportSize({width, height: 844});
      await page.locator('[data-entry]').first().waitFor();
      assert(await page.locator('#language-select').isVisible());
      await noOverflow(page);
      await page.screenshot({path: `artifacts/mobile-${width}-en.png`, fullPage: true});
    }
    await language(page, 'zh-CN');
    await page.locator('[data-entry]').first().waitFor();
    assert.match(await page.locator('#page-title').innerText(), /缓存工作台/);
    await page.screenshot({path: 'artifacts/mobile-zh.png', fullPage: true});

    const protectedServer = await server(true);
    const auth = await browser.newContext({viewport: {width: 390, height: 844}});
    const login = await auth.newPage();
    login.on('pageerror', error => errors.push(error.message));
    assert.equal((await auth.request.get(protectedServer.origin + '/api/entries')).status(), 401);
    await login.goto(protectedServer.invite);
    await login.locator('#activate-form').waitFor();
    await language(login, 'en');
    await login.locator('#activate-form').waitFor();
    await login.locator('[name=password]').fill('browser-test-password-only');
    await login.locator('[name=confirm]').fill('browser-test-password-only');
    await login.locator('#activate-form button').click();
    await login.locator('[data-entry]').first().waitFor();
    assert.equal((await auth.request.get(protectedServer.origin + '/api/entries')).status(), 200);
    await login.goto(protectedServer.origin + '/access');
    await login.locator('#logout').waitFor();
    assert.match(await login.locator('#account-content').innerText(), /Account settings/);
    await noOverflow(login);
    await login.screenshot({path: 'artifacts/account-en.png', fullPage: true});
    await login.locator('#logout').click();
    await login.locator('#login-form').waitFor();
    await login.locator('[name=username]').fill('owner');
    await login.locator('[name=password]').fill('wrong-password-for-test');
    await login.locator('#login-form button').click();
    await login.waitForFunction(() => document.querySelector('#account-message').textContent.includes('Incorrect username or password'));
    await login.locator('[name=password]').fill('browser-test-password-only');
    await login.locator('#login-form button').click();
    await login.locator('[data-entry]').first().waitFor();
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: languages, details, filters, mobile, activation, authentication.');
  } finally {
    if (browser) await browser.close();
    for (const child of servers) {
      if (child.exitCode === null) {const stopped = once(child, 'exit'); child.kill('SIGTERM'); await stopped;}
    }
  }
})().catch(error => {console.error(error.message); process.exitCode = 1;});
