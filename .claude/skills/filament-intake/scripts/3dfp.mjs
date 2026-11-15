// 3dfilamentprofiles.com driver: search, add to My Spools, export slicer profiles.
//
//   node 3dfp.mjs login                       # headed; run once per profile dir
//   node 3dfp.mjs search "Inland Frosty Amethyst" ["Insignia PLA+"...]
//   node 3dfp.mjs add 30173 4427 ...          # filament detail ids -> My Spools
//   node 3dfp.mjs export 30173 4427 ...       # -> $OUT/<id>__<profile name>.json
//
// Env: PRINTER (default H2C), SLICER (default "Bambu Studio"), NOZZLES (default
// "0.2,0.4,0.6,0.8"), OUT (default ./profiles), PROFILE_DIR (default
// ~/.cache/bambuddy-3dfp-profile), CHROME (default: newest ~/.cache/ms-playwright chromium),
// PLAYWRIGHT (path to playwright's index.mjs; default: first one under ~/.npm/_npx).
import fs from 'fs';
import os from 'os';
import path from 'path';
import { execSync, execFileSync } from 'child_process';

const home = os.homedir();
const findPlaywright = () => execSync(`ls -d ${home}/.npm/_npx/*/node_modules/playwright/index.mjs | head -1`).toString().trim();
const findChrome = () => execSync(`ls -d ${home}/.cache/ms-playwright/chromium-*/chrome-linux*/chrome | sort -V | tail -1`).toString().trim();
const { chromium } = await import(process.env.PLAYWRIGHT || findPlaywright());

const PRINTER = process.env.PRINTER || 'H2C';
const SLICER = process.env.SLICER || 'Bambu Studio';
const NOZZLES = (process.env.NOZZLES || '0.2,0.4,0.6,0.8').split(',').map(n => `${n} mm`);
const OUT = process.env.OUT || 'profiles';
// The profile holds the site's session cookies, so it lives outside the repo.
const PROFILE_DIR = path.resolve(process.env.PROFILE_DIR || path.join(home, '.cache', 'bambuddy-3dfp-profile'));
fs.mkdirSync(PROFILE_DIR, { recursive: true, mode: 0o700 });
const SITE = 'https://3dfilamentprofiles.com';
const [cmd, ...args] = process.argv.slice(2);

// Headed passes Vercel's bot challenge on a fresh profile; after that the
// cookie lets headless through. Only `login` needs headed.
// Vercel's firewall answers 403 to the "HeadlessChrome" user agent (seen
// 2026-11-01 on every page, including ones the cookie used to pass), so
// headless runs present a normal Chrome UA.
const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
  headless: cmd !== 'login', executablePath: process.env.CHROME || findChrome(),
  userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
});
// The export builds a blob and clicks an <a download>. Chromium in this
// environment dies on that download intermittently ("Target page, context or
// browser has been closed"), so read the blob in-page instead.
await ctx.addInitScript(() => {
  window.__dl = [];
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (this.download && this.href.startsWith('blob:')) {
      fetch(this.href).then(r => r.text()).then(text => window.__dl.push({ name: this.download, text }));
      return;
    }
    return click.call(this);
  };
});
const p = ctx.pages()[0] || await ctx.newPage();

try {
  if (cmd === 'login') {
    // The 1Password item's username field is empty; the account email is the user's.
    const email = process.env.FP_EMAIL;
    if (!email) throw new Error('set FP_EMAIL');
    const pw = execFileSync('op.exe', ['item', 'get', '3D Filament Profiles', '--vault', 'Private', '--fields', 'password', '--reveal']).toString().trim();
    await p.goto(`${SITE}/login`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(5000);
    await p.fill('input[name=email]', email);
    await p.fill('input[name=password]', pw);
    await p.getByRole('button', { name: 'Sign In', exact: true }).click();
    await p.waitForTimeout(6000);
    await p.goto(`${SITE}/my/spools`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(4000);
    console.log(p.url().includes('/login') ? 'LOGIN FAILED' : 'logged in');
  } else if (cmd === 'search') {
    // Only ?q= filters; ?search= and ?brand= are silently ignored.
    for (const q of args) {
      await p.goto(`${SITE}/filaments?q=${encodeURIComponent(q)}`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(6000);
      const rows = await p.$$eval('a[href*="/filament/details/"]', els => [...new Set(els.map(e =>
        e.href.split('/').pop() + '\t' + (e.closest('tr') || e).innerText.replace(/\s+/g, ' ').trim().slice(0, 140)))]);
      console.log(`## ${q}\n` + rows.filter(r => r.includes(' — ')).join('\n'));
    }
  } else if (cmd === 'add') {
    // Adds silently: no dialog, no toast worth reading. Verify with `export`.
    for (const id of args) {
      await p.goto(`${SITE}/filament/details/${id}`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(4000);
      const btn = p.getByRole('button', { name: 'Add to My Spools' }).first();
      if (await btn.count()) { await btn.click(); await p.waitForTimeout(2500); console.log(id, 'added'); }
      else console.log(id, 'already in My Spools');
    }
  } else if (cmd === 'export') {
    fs.mkdirSync(OUT, { recursive: true });
    let failed = 0;
    for (const id of args) {
      // The per-spool page, not the My Spools list: a list row that groups
      // several spools of one filament hides the export button behind an
      // "Expand the group" control, while this page has one per spool.
      try {
        await p.goto(`${SITE}/my/spools/details/${id}`, { waitUntil: 'domcontentloaded' }); await p.waitForTimeout(5000);
      } catch (e) {
        failed += NOZZLES.length; console.log(id, 'FAILED', e.message.split('\n')[0]); continue;
      }
      if (p.url().includes('/login')) throw new Error('not logged in: run `login` first');
      for (const nz of NOZZLES) try {
        const btn = p.getByRole('button', { name: 'Export slicer profile' }).first();
        if (!(await btn.count())) throw new Error('not in My Spools (run add first)');
        await btn.click(); await p.waitForTimeout(1500);
        const dlg = p.locator('[role=dialog]').last();
        for (const [i, t] of [[0, SLICER], [1, PRINTER], [2, nz]]) {
          await dlg.getByRole('combobox').nth(i).click(); await p.waitForTimeout(400);
          await p.getByRole('option', { name: t, exact: true }).click(); await p.waitForTimeout(500);
        }
        const ack = dlg.getByRole('checkbox').last();
        if ((await ack.getAttribute('aria-checked')) === 'false') await ack.click();
        await p.evaluate(() => { window.__dl = []; });
        await dlg.getByRole('button', { name: /^Download/ }).click();
        await p.waitForFunction(() => window.__dl.length > 0, null, { timeout: 15000 });
        const d = (await p.evaluate(() => window.__dl))[0];
        const file = `${OUT}/${id}__${d.name.replace(/\//g, '-')}`;
        fs.writeFileSync(file, d.text);
        console.log(id, nz, file);
        await p.keyboard.press('Escape'); await p.waitForTimeout(600);
      } catch (e) {
        failed++; console.log(id, nz, 'FAILED', e.message.split('\n')[0]);
        await p.keyboard.press('Escape').catch(() => {});
      }
    }
    if (failed) process.exitCode = 1;
  } else {
    console.log('usage: node 3dfp.mjs login|search <q>...|add <id>...|export <id>...');
    process.exitCode = 2;
  }
} finally {
  await ctx.close();
}
