// Takes the README/docs screenshots of the real page, served by the silent "null" backend (no sound is made).
// Usage: node tools/screenshots.js   (needs playwright; starts and stops its own server on port 8781)
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
const root = path.resolve(__dirname, '..');
const out = path.join(root, 'docs', 'screenshots');
const PORT = 8781;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  try { await fetch(`http://127.0.0.1:${PORT}/`); console.error(`port ${PORT} is already in use; stop that server first`); process.exit(1); } catch { /* free */ }
  const env = { ...process.env, SPATIAL_LAB_CONFIG: '/tmp/sl-shots-config.yml', SPATIAL_LAB_DATA: '/tmp/sl-shots-data' };
  const srv = spawn('python3', ['-m', 'spatial_lab', 'serve', '--backend', 'null', '--port', String(PORT)], { cwd: root, env, stdio: 'ignore' });
  await sleep(2500);
  const b = await chromium.launch({ args: ['--use-gl=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await p.goto(`http://127.0.0.1:${PORT}/`);
  await p.waitForTimeout(1200);
  const open = (names) => p.evaluate((n) => document.querySelectorAll('details').forEach((d) => { d.open = n.includes(d.querySelector('summary').textContent.trim()); }), names);
  const setSlider = (id, v) => p.evaluate(([i, x]) => { const e = document.getElementById(i); e.value = x; e.dispatchEvent(new Event('input')); }, [id, v]);

  await open(['Output & safety', 'Object', 'Source', 'Speakers']);
  await setSlider('master', -40);
  await p.click('#btnMute');                      // unmute (silent backend)
  await p.waitForTimeout(500);

  // 1. overview, object on the "over the top" path
  const play = async (id, keep) => { await open(['Paths']); await p.selectOption('#presets', id); await p.click('#pathPlay'); await open(keep); };
  await play('height_arc', ['Output & safety', 'Object', 'Speakers']); await p.waitForTimeout(4200);
  await p.screenshot({ path: path.join(out, 'overview.png') });

  // 2. top view, lap path
  await p.click('[data-cam=top]'); await play('lap', ['Output & safety', 'Object', 'Speakers']); await p.waitForTimeout(7000);
  await p.screenshot({ path: path.join(out, 'top-lap.png') });

  // 3. seat view, object in front and to the left
  await open(['Paths']); await p.click('#pathStop'); await open(['Output & safety', 'Object', 'Speakers']); await p.click('[data-cam=seat]');
  await p.keyboard.press('ArrowLeft'); await p.keyboard.press('ArrowLeft'); await p.waitForTimeout(900);
  await p.screenshot({ path: path.join(out, 'seat-view.png') });

  // 4. labs: front pair with 1 ms on FR (summing localisation), sidebar only
  await p.click('[data-cam=overview]');
  await open(['Speakers', 'Labs (chosen-speaker mode)']);
  await p.click('#modeSeg [data-mode=manual]');
  await p.click('#labSets button:nth-child(1)');
  await p.evaluate(() => { const rows = [...document.querySelectorAll('.spk')]; const fr = rows.find((r) => r.querySelector('.nm').textContent === 'FR'); const d = fr.querySelectorAll('input[type=range]')[1]; d.value = 1; d.dispatchEvent(new Event('input')); });
  await open(['Speakers', 'Labs (chosen-speaker mode)']);
  await p.evaluate(() => { const a = document.getElementById('side'); a.scrollTop = a.scrollHeight; });
  await p.waitForTimeout(1500);
  await p.screenshot({ path: path.join(out, 'labs.png') });

  // 5. phone width
  await p.setViewportSize({ width: 420, height: 900 }); await open(['Output & safety', 'Object']); await p.waitForTimeout(800);
  await p.screenshot({ path: path.join(out, 'phone.png') });
  await b.close(); srv.kill();
})();
