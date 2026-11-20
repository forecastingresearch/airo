// Run on the controller. The browser reads HTTPS through an SSH tunnel to the web guest.
const { execFileSync } = require('node:child_process');
const { X509Certificate, createHash } = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

async function main() {
  const [config, packages] = process.argv.slice(2);
  if (!config || !packages) throw Error('Usage: node browser_staging.cjs SSH_CONFIG NODE_MODULES');
  const modules = path.resolve(packages);
  const { chromium } = require(path.join(modules, 'playwright-core'));
  const domains = ['airo', 'eeai', 'rsi'].map(site => site + '.forecast.test');
  const ssh = (...args) => execFileSync('ssh', ['-F', config, ...args], { encoding: 'utf8' });
  const certificate = new X509Certificate(ssh('stage-web', 'cat /var/lib/fri-acceptance/tls.pem'));
  const spki = createHash('sha256').update(certificate.publicKey.export({ type: 'spki', format: 'der' })).digest('base64');
  const reader = JSON.parse(ssh('stage-web', 'cat /var/lib/fri-acceptance/eeai-reader.json'));
  const cdn = {
    'react@18.3.1/umd/react.development.js': 'react/umd/react.development.js',
    'react-dom@18.3.1/umd/react-dom.development.js': 'react-dom/umd/react-dom.development.js',
    '@babel/standalone@7.29.0/babel.min.js': '@babel/standalone/babel.min.js',
    '@popperjs/core@2.11.8/dist/umd/popper.min.js': '@popperjs/core/dist/umd/popper.min.js',
    'tippy.js@6.3.7/dist/tippy-bundle.umd.min.js': 'tippy.js/dist/tippy-bundle.umd.min.js',
    'tippy.js@6.3.7/dist/tippy.css': 'tippy.js/dist/tippy.css',
  };
  const tmp = fs.mkdtempSync('/var/tmp/fri-browser-');
  const mux = path.join(tmp, 'mux');
  const server = net.createServer();
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  let browser;
  try {
    execFileSync('ssh', ['-F', config, '-o', 'ExitOnForwardFailure=yes', '-M', '-S', mux, '-fN',
                         '-L', '127.0.0.1:' + port + ':127.0.0.1:443', 'stage-web'], { stdio: 'inherit' });
    const browserEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(https?|all|no)_proxy$/i.test(key)));
    browser = await chromium.launch({
      env: browserEnv,
      executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
      args: ['--no-proxy-server', '--disable-dev-shm-usage', '--ignore-certificate-errors-spki-list=' + spki,
             '--host-resolver-rules=' + domains.map(host => 'MAP ' + host + ' 127.0.0.1:' + port).join(',')],
    });
    const context = await browser.newContext({ httpCredentials: { ...reader, origin: 'https://eeai.forecast.test' } });
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (domains.includes(url.hostname)) return route.continue();
      const asset = url.hostname === 'unpkg.com' && cdn[url.pathname.slice(1)];
      if (asset) return route.fulfill({ path: path.join(modules, asset) });
      return route.abort(); // Exclude live telemetry, web fonts and any unreviewed third-party scripts.
    });

    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('https://airo.forecast.test/');
    await page.getByRole('button', { name: /Definitions & methodology/ }).click();
    await page.getByRole('heading', { name: 'Definitions & methodology' }).waitFor();
    console.log('AIRO opened the methodology dialog');

    await page.goto('https://eeai.forecast.test/');
    const next = page.locator('button[data-k][aria-pressed="false"]').first();
    const control = await next.getAttribute('data-k');
    await next.click();
    await page.locator(`button[data-k="${control}"][aria-pressed="true"]`).waitFor();
    console.log('EEAI authenticated and changed a forecast control');

    await page.goto('https://rsi.forecast.test/');
    await page.locator('#cards button').first().click();
    await page.locator('dialog[open]').waitFor();
    console.log('RSI opened a survey question');
    if (errors.length) throw Error('Dashboard JavaScript errors: ' + errors.join(', '));
  } finally {
    if (browser) await browser.close();
    if (fs.existsSync(mux)) ssh('-S', mux, '-O', 'exit', 'stage-web');
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
