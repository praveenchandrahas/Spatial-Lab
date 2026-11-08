import * as THREE from './vendor/three.module.js';
import { OrbitControls } from './vendor/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const now = () => performance.now();

let ws = null, L = null, S = null, OUT = null, INIT = null;
let presets = [], recordings = [], pending = null, canRoute = false;
let built = false, lastMuted = true;

// ------------------------------------------------------------------ connection

function send(o) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(o));
}
const cmd = (c, extra = {}) => send({ t: 'cmd', c, ...extra });

let queued = null, queueTimer = null;
function queue(patch) {
  if (!queued) queued = {};
  for (const [k, v] of Object.entries(patch)) {
    if (k === 'spk') {
      queued.spk = queued.spk || {};
      for (const [n, d] of Object.entries(v)) queued.spk[n] = { ...(queued.spk[n] || {}), ...d };
    } else queued[k] = v;
  }
  if (!queueTimer) queueTimer = setTimeout(() => {
    queueTimer = null;
    const p = queued; queued = null;
    send({ t: 'set', patch: p });
  }, 40);
}

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onerror = () => {};
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === 'init') onInit(m);
    else if (m.t === 'tick') { S = m.s; OUT = m.out; onTick(); }
    else if (m.t === 'event') toast(m.msg, m.level);
    else if (m.t === 'recs') { recordings = m.recordings; pending = m.pending; renderRecs(); }
  };
}
function setConn(ok) {
  const c = $('conn');
  c.textContent = ok ? 'connected' : 'disconnected';
  c.className = 'pill ' + (ok ? 'good' : 'bad');
  const b = $('banner');
  if (!ok) { b.hidden = false; b.textContent = 'Disconnected from the Spatial-Lab server. Audio is muted. Reconnecting…'; }
  else b.hidden = true;
}
setInterval(() => send({ t: 'hb' }), 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) send({ t: 'hb' }); });

let toastTimer = null;
function toast(msg, level = 'info') {
  const t = $('toast');
  t.textContent = msg; t.className = 'show ' + (level === 'error' ? 'error' : level === 'warn' ? 'warn' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ''; }, level === 'error' ? 8000 : 3500);
}

// ------------------------------------------------------------------ init + controls

function onInit(m) {
  INIT = m; L = m.layout; S = m.snap; OUT = m.out; presets = m.presets; recordings = m.recordings; canRoute = m.can_route;
  $('layoutName').textContent = `${L.name} · ${m.backend}`;
  if (!built) { built = true; buildUI(); buildScene(); }
  $('master').max = String(m.cfg.ceiling_db);
  renderRecs();
  onTick();
}

function sync(el, v) {
  if (!el || document.activeElement === el || now() - (el._t || 0) < 700) return;
  if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
}

function slider(id, key, o = {}) {
  const el = $(id), out = $(id + 'V');
  const fmt = o.fmt || ((v) => String(v));
  el.addEventListener('input', () => {
    el._t = now();
    const v = parseFloat(el.value);
    if (out) out.textContent = fmt(v);
    if (o.cmd) o.cmd(v); else queue({ [key]: v });
  });
  (ctls.push(() => {
    if (!S) return;
    const v = o.get ? o.get(S) : S.params[key];
    sync(el, v);
    if (out && !(document.activeElement === el)) out.textContent = fmt(parseFloat(v));
  }));
}
const ctls = [];

function seg(id, attr, get, set) {
  const el = $(id);
  el.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => set(b.dataset[attr])));
  ctls.push(() => { if (S) el.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset[attr] === get(S))); });
}

function buildUI() {
  const f1 = (v) => v.toFixed(1), f0 = (v) => v.toFixed(0), f2 = (v) => v.toFixed(2);
  slider('master', 'master_db', { fmt: (v) => v.toFixed(1) + ' dB' });
  slider('spread', 'spread_deg', { fmt: (v) => f0(v) + '°' });
  slider('corr', 'corr', { fmt: f2 });
  slider('distExp', 'dist_exp', { fmt: f2 });
  slider('airEx', 'air_ex', { fmt: (v) => '×' + f1(v) });
  slider('dopScale', 'doppler_scale', { fmt: (v) => '×' + f1(v) });
  slider('burstOn', 'burst_on_ms', { fmt: (v) => v === 0 ? 'continuous' : f0(v) + ' ms' });
  slider('burstOff', 'burst_off_ms', { fmt: (v) => f0(v) + ' ms' });
  slider('speed', 'speed', { fmt: (v) => '×' + f2(v), get: () => parseFloat($('speed').value) || 1,
    cmd: (v) => { if (S && S.motion) cmd('motion_speed', { speed: v }); } });
  $('speed').value = '1'; $('speedV').textContent = '×1.00';

  for (const [id, key] of [['law', 'law'], ['align', 'align']]) {
    const el = $(id);
    el.addEventListener('change', () => { el._t = now(); queue({ [key]: el.value }); });
    ctls.push(() => S && sync(el, S.params[key]));
  }
  $('doppler').addEventListener('change', (e) => { e.target._t = now(); queue({ doppler: e.target.checked }); });
  ctls.push(() => S && sync($('doppler'), S.params.doppler));
  $('hp').addEventListener('change', (e) => cmd('hp', { hz: parseFloat(e.target.value) }));
  ctls.push(() => S && sync($('hp'), String(Math.round(S.hp_hz))));

  seg('routeSeg', 'route', (s) => s.params.out_mode, (v) => cmd('route', { mode: v }));
  seg('srcSeg', 'src', (s) => s.params.src, (v) => queue({ src: v }));
  seg('modeSeg', 'mode', (s) => s.params.mode, (v) => queue({ mode: v }));

  $('btnMute').addEventListener('click', toggleMute);
  $('btnPanic').addEventListener('click', panic);

  // object nudges
  document.querySelectorAll('[data-n]').forEach((b) => b.addEventListener('click', () => nudge(b.dataset.n, 0.1)));
  $('btnCentre').addEventListener('click', () => send({ t: 'pos', p: [0, Math.min(2.4, L.room.y[1] - 0.4), 0.15] }));

  // file
  $('fileIn').addEventListener('change', async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $('fileName').textContent = 'uploading…';
    try {
      const r = await fetch('/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f });
      const j = await r.json();
      if (!j.ok) { $('fileName').textContent = j.error || 'failed'; return; }
      $('fileName').textContent = `${f.name} (${j.seconds} s)`;
      queue({ src: 'file' });
    } catch (err) { $('fileName').textContent = 'upload failed'; }
  });
  ctls.push(() => { if (S && S.file) $('fileName').textContent = S.file; });

  // paths
  const sel = $('presets');
  presets.forEach((p) => { const o = document.createElement('option'); o.value = p.id; o.textContent = `${p.label} (${p.period} s)`; sel.appendChild(o); });
  const note = () => { const p = presets.find((x) => x.id === sel.value); $('pathNote').textContent = p ? p.note : ''; };
  sel.addEventListener('change', note); note();
  $('pathPlay').addEventListener('click', playPath);
  $('pathStop').addEventListener('click', () => cmd('stop_motion'));
  $('recStart').addEventListener('click', () => cmd('rec_start'));
  $('recStop').addEventListener('click', () => cmd('rec_stop'));
  $('recSave').addEventListener('click', () => cmd('rec_save', { name: $('recName').value }));
  $('recDiscard').addEventListener('click', () => cmd('rec_discard'));
  $('recPlay').addEventListener('click', () => cmd('replay_pending', { speed: speedVal() }));

  buildSpeakers();
  buildLabs();
  document.querySelectorAll('[data-cam]').forEach((b) => b.addEventListener('click', () => camera(b.dataset.cam)));
  $('resetTweaks').addEventListener('click', () => cmd('reset_tweaks'));
}

const speedVal = () => parseFloat($('speed').value) || 1;
function playPath() { cmd('traj', { id: $('presets').value, speed: speedVal(), loop: $('pathLoop').checked }); }
function toggleMute() { if (S && S.muted) cmd('unmute'); else cmd('mute'); }
function panic() { cmd('panic'); }

function nudge(dir, step) {
  if (!S) return;
  const p = [...S.pos];
  const ax = { x: 0, y: 1, z: 2 }[dir[1]], sg = dir[0] === '+' ? 1 : -1;
  p[ax] += sg * step;
  send({ t: 'pos', p });
}

function renderRecs() {
  $('recPending').hidden = !pending;
  const el = $('recList'); el.textContent = '';
  for (const r of recordings) {
    const d = document.createElement('div');
    const n = document.createElement('span'); n.textContent = `${r.name} (${r.duration} s)`;
    const p = document.createElement('button'); p.textContent = 'Play'; p.onclick = () => cmd('replay', { name: r.name, speed: speedVal(), loop: $('pathLoop').checked });
    const x = document.createElement('button'); x.textContent = 'Delete'; x.onclick = () => { if (confirm(`Delete recording '${r.name}'?`)) cmd('rec_delete', { name: r.name }); };
    d.append(n, p, x); el.appendChild(d);
  }
}

// ---- speakers panel

const spkRows = {};
function buildSpeakers() {
  const panned = L.speakers.filter((s) => s.role !== 'sub');
  const chips = $('chips');
  for (const s of panned) {
    const b = document.createElement('button');
    b.textContent = s.name; b.className = s.role === 'height' ? 'h' : '';
    b.title = 'Include in the chosen set';
    b.addEventListener('click', () => {
      const cur = new Set(S.params.manual_set);
      if (cur.has(s.name)) { if (cur.size > 1) cur.delete(s.name); } else cur.add(s.name);
      const order = panned.map((x) => x.name).filter((n) => cur.has(n));
      queue({ manual_set: order, mode: 'manual' });
    });
    chips.appendChild(b); spkRows['chip_' + s.name] = b;
  }
  const tbl = $('spkTable');
  for (const s of L.speakers) {
    const row = document.createElement('div'); row.className = 'spk';
    const nm = document.createElement('div'); nm.className = 'nm ' + (s.role === 'height' ? 'h' : 'b'); nm.textContent = s.name;
    nm.title = `${s.port_name} · port ${s.port}` + (s.az != null ? ` · az ${s.az}° el ${s.el}°` : '');
    row.appendChild(nm);
    if (s.role === 'sub') {
      const t = document.createElement('div'); t.className = 'small mut'; t.style.gridColumn = '2 / -1';
      t.textContent = 'LFE output: always silent in Spatial-Lab'; row.appendChild(t); tbl.appendChild(row); continue;
    }
    const mt = document.createElement('div'); mt.className = 'meter ' + (s.role === 'height' ? 'h' : '');
    const bar = document.createElement('i'); const pk = document.createElement('i'); pk.className = 'pk';
    mt.append(bar, pk);
    const pkt = document.createElement('div'); pkt.className = 'small mut'; pkt.style.textAlign = 'right';
    const bM = mkBtn('M', 'Mute this speaker'), bS = mkBtn('S', 'Solo this speaker'), bP = mkBtn('ø', 'Invert polarity');
    row.append(mt, pkt, bM, bS, bP);
    const sub = document.createElement('div'); sub.className = 'sub2';
    const tl = document.createElement('label'); tl.append('trim ');
    const tr = document.createElement('input'); tr.type = 'range'; tr.min = -30; tr.max = 0; tr.step = 0.5; const tv = document.createElement('span'); tl.append(tr, tv);
    const dl = document.createElement('label'); dl.append('delay ');
    const dr = document.createElement('input'); dr.type = 'range'; dr.min = 0; dr.max = 20; dr.step = 0.05; const dv = document.createElement('span'); dl.append(dr, dv);
    sub.append(tl, dl); row.appendChild(sub);
    tbl.appendChild(row);
    bM.onclick = () => queue({ spk: { [s.name]: { mute: !S.params.spk[s.name].mute } } });
    bS.onclick = () => queue({ solo: S.params.solo === s.name ? null : s.name });
    bP.onclick = () => queue({ spk: { [s.name]: { invert: !S.params.spk[s.name].invert } } });
    tr.addEventListener('input', () => { tr._t = now(); tv.textContent = (+tr.value).toFixed(1); queue({ spk: { [s.name]: { trim_db: +tr.value } } }); });
    dr.addEventListener('input', () => { dr._t = now(); dv.textContent = (+dr.value).toFixed(2); queue({ spk: { [s.name]: { delay_ms: +dr.value } } }); });
    spkRows[s.name] = { bar, pk, pkt, bM, bS, bP, tr, tv, dr, dv, port: s.port };
  }
}
function mkBtn(t, title) { const b = document.createElement('button'); b.textContent = t; b.title = title; return b; }

function updateSpeakers() {
  const m = S.meters;
  for (const s of L.speakers) {
    const r = spkRows[s.name]; if (!r) continue;
    const p = S.params.spk[s.name];
    const rms = m.rms[s.port], pk = m.peak[s.port];
    r.bar.style.width = clamp((rms + 90) / 90, 0, 1) * 100 + '%';
    r.pk.style.left = clamp((pk + 90) / 90, 0, 1) * 100 + '%';
    r.pkt.textContent = pk <= -89 ? '–' : pk.toFixed(0);
    r.bM.classList.toggle('on', p.mute); r.bS.classList.toggle('on', S.params.solo === s.name); r.bP.classList.toggle('on', p.invert);
    sync(r.tr, p.trim_db); sync(r.dr, p.delay_ms);
    if (document.activeElement !== r.tr) r.tv.textContent = p.trim_db.toFixed(1);
    if (document.activeElement !== r.dr) r.dv.textContent = p.delay_ms.toFixed(2);
  }
  const set = new Set(S.params.manual_set);
  for (const s of L.speakers) { const c = spkRows['chip_' + s.name]; if (c) c.classList.toggle('on', set.has(s.name)); }
}

// ---- labs

let sweepTimer = null;
function buildLabs() {
  const sets = [['Front pair', ['FL', 'FR']], ['Phantom centre vs C', ['FL', 'FR', 'C']], ['Centre alone', ['C']],
    ['Heights pair', ['TFL', 'TFR']], ['Surrounds', ['SL', 'SR']], ['Left bed + height', ['FL', 'TFL']], ['Left front + surround', ['FL', 'SL']]];
  const names = new Set(L.speakers.map((s) => s.name));
  for (const [label, set] of sets) {
    if (!set.every((n) => names.has(n))) continue;
    const b = document.createElement('button'); b.textContent = label;
    b.onclick = () => queue({ manual_set: set, mode: 'manual' });
    $('labSets').appendChild(b);
  }
  document.querySelectorAll('[data-sweep]').forEach((b) => b.addEventListener('click', () => startSweep(b.dataset.sweep)));
  $('sweepStop').addEventListener('click', stopSweep);
}
function stopSweep() { clearInterval(sweepTimer); sweepTimer = null; }
function startSweep(kind) {
  stopSweep();
  const set = S.params.manual_set;
  const B = set[1];
  if (kind !== 'width' && !B) { toast('Choose at least two speakers first', 'warn'); return; }
  queue({ mode: 'manual' });
  const dur = kind === 'delay' ? 12000 : 10000, t0 = now();
  sweepTimer = setInterval(() => {
    const u = clamp((now() - t0) / dur, 0, 1);
    if (kind === 'delay') queue({ spk: { [B]: { delay_ms: 3 * u } } });
    else if (kind === 'level') queue({ spk: { [B]: { trim_db: -20 + 20 * u } } });
    else queue({ corr: u });
    if (u >= 1) stopSweep();
  }, 100);
}

function labNote() {
  const p = S.params, set = p.manual_set;
  if (p.mode !== 'manual') return 'Switch to “Chosen speakers” to use the labs.';
  if (set.length < 2) return `Single speaker: ${set[0]}.`;
  const [a, b] = set, A = p.spk[a], B = p.spk[b];
  const dt = Math.abs(B.delay_ms - A.delay_ms), dl = Math.abs(B.trim_db - A.trim_db);
  const inv = A.invert !== B.invert;
  const lines = [`${a} / ${b}: delay difference ${dt.toFixed(2)} ms, level difference ${dl.toFixed(1)} dB${inv ? ', one polarity inverted' : ''}.`];
  if (inv) lines.push('Opposite polarity: the two signals cancel where they overlap, so the image becomes vague or seems to come from outside the pair, and low frequencies thin out.');
  else if (dt < 0.05 && dl < 0.5) lines.push(`Equal level and timing: a phantom image midway between ${a} and ${b}.`);
  if (!inv) {
    if (dt >= 0.05 && dt < 1) lines.push('Under about 1 ms (summing localisation): the image moves toward the earlier speaker; near 1 ms it sits almost on it.');
    else if (dt >= 1 && dt <= 30) lines.push('About 1 to 30 ms (precedence effect): the image stays on the earlier speaker; the later one adds mostly width and tone.');
    else if (dt > 30) lines.push('Over about 30 ms: the later speaker begins to be heard as a separate echo.');
    if (dt >= 0.05 && dl < 3 && dt < 6) lines.push(`Comb filtering: first notch near ${Math.round(1000 / (2 * dt))} Hz (for equal levels, idealised; the room changes it).`);
    if (dl >= 0.5 && dt < 0.05) lines.push('Level difference only: the image moves toward the louder speaker (about 1 dB per degree for a symmetric pair, roughly).');
  }
  if (p.corr > 0.02) lines.push(`Width ${p.corr.toFixed(2)}: each speaker gets a decorrelated copy, so the image widens and loses its point source.`);
  lines.push('These are textbook approximations to guide listening, not measurements.');
  return lines.join(' ');
}

// ------------------------------------------------------------------ per-tick UI

function onTick() {
  if (!S) return;
  ctls.forEach((f) => f());
  const mb = $('btnMute');
  mb.textContent = S.muted ? 'Unmute' : 'Mute'; mb.classList.toggle('live', !S.muted);
  if (S.muted && !lastMuted && S.mute_reason && !['user', 'panic', 'shutdown'].includes(S.mute_reason)) toast('Muted: ' + S.mute_reason, 'warn');
  lastMuted = S.muted;
  $('levelPill').textContent = S.muted ? 'muted' : `level ${S.level_db.toFixed(0)} dB`;
  $('loadPill').textContent = `load ${(S.load * 100).toFixed(0)}%` + (S.clips ? ` · clips ${S.clips}` : '');
  $('loadPill').className = 'pill' + (S.load > 0.7 || S.clips ? ' bad' : '');
  $('ceilNote').textContent = `Ceiling ${S.ceiling_db.toFixed(0)} dBFS. Fade-in ${S.fade_in_s.toFixed(0)} s. Starts muted. Change the ceiling with: spatial-lab set-ceiling DB, then restart.`;
  $('routeNote').textContent = canRoute
    ? (S.params.out_mode === 'direct' ? 'Direct: straight to the interface, with distance alignment (Auto).' : 'Through: CamillaDSP applies its own bass management, EQ, delays and trims.')
    : 'This backend has no routing.';
  $('routeSeg').querySelectorAll('button').forEach((b) => { b.disabled = !canRoute; });
  renderOut();
  $('roAz').textContent = S.az.toFixed(0) + '°'; $('roEl').textContent = S.el.toFixed(0) + '°'; $('roR').textContent = S.r.toFixed(2) + ' m';
  $('roXYZ').textContent = S.pos.map((v) => v.toFixed(1)).join(' / ');
  const re = S.meters.re, rv = S.meters.rv;
  $('imgRE').textContent = re ? `Energy vector: az ${re.az.toFixed(0)}°, el ${re.el.toFixed(0)}°, focus ${re.mag.toFixed(2)}` : 'no signal';
  $('imgRV').textContent = rv ? `Velocity vector: az ${rv.az.toFixed(0)}°, el ${rv.el.toFixed(0)}°` : 'Velocity vector: undefined (signals cancel or out of phase)';
  const mt = S.motion;
  $('pathPlay').classList.toggle('live', !!mt);
  $('recStart').classList.toggle('danger', !!S.recording);
  $('recT').textContent = S.recording ? `recording ${S.recording.t.toFixed(1)} s` : (mt ? `${mt.id} ${mt.t.toFixed(1)}/${mt.duration} s` : '');
  if (S.params.mode === 'identify' && S.id_current) $('dragHint').textContent = `Identify: now playing ${S.id_current}`;
  else $('dragHint').textContent = 'Drag the amber ball. Shift+drag moves it up/down.';
  updateSpeakers();
  $('labNote').textContent = labNote();
}

function renderOut() {
  const el = $('outStatus'); el.textContent = '';
  if (!OUT) return;
  const add = (t, c) => { const d = document.createElement('div'); d.textContent = t; if (c) d.className = c; el.appendChild(d); };
  if (OUT.backend === 'pipewire') {
    add(OUT.alive ? 'pw-cat running' : 'pw-cat NOT running', OUT.alive ? 'ok' : 'err');
    if (OUT.sink_present === false) add('USB interface sink not found', 'err');
    if (OUT.cdsp_present === false) add('CamillaDSP is not running (Through mode will be silent)', 'warn');
    if (OUT.mpv_present) add('mpv is playing: stop it before testing', 'warn');
  } else add(`Backend: ${OUT.backend} (no real sound output)`, 'warn');
  (OUT.warnings || []).forEach((w) => add(w, 'warn'));
}

// ------------------------------------------------------------------ 3D scene

let scene, camera3, renderer, controls, objMesh, hitMesh, floorMark, dropLine, trail, trailPts = [], rays = [], spkMeshes = {}, seatGroup;
let camName = 'overview';
const tv = (p) => new THREE.Vector3(p[0], p[2], -p[1]);
const fromV = (v) => [v.x, -v.z, v.y];
let dragging = false, vertDrag = false, dragOffset = new THREE.Vector3(), dragPlane = new THREE.Plane(), lastPosSent = 0;

function labelSprite(text, color) {
  const c = document.createElement('canvas'); c.width = 256; c.height = 64;
  const g = c.getContext('2d'); g.font = '600 38px system-ui, sans-serif'; g.fillStyle = color; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(text, 128, 34);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthTest: false }));
  sp.scale.set(0.6, 0.15, 1); sp.renderOrder = 10; return sp;
}

function buildScene() {
  const canvas = $('view');
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  scene = new THREE.Scene(); scene.background = new THREE.Color(0x12161b);
  camera3 = new THREE.PerspectiveCamera(50, 1, 0.02, 60);
  controls = new OrbitControls(camera3, canvas);
  controls.enableDamping = true; controls.minDistance = 0.05; controls.maxDistance = 20;
  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const dl = new THREE.DirectionalLight(0xffffff, 1.1); dl.position.set(2, 4, 3); scene.add(dl);

  const R = L.room, W = R.width_m, D = R.depth_m, H = R.height_m;
  const cx = (R.x[0] + R.x[1]) / 2, cy = (R.z[0] + R.z[1]) / 2, cz = -(R.y[0] + R.y[1]) / 2;
  const box = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(W, H, D)), new THREE.LineBasicMaterial({ color: 0x4a5a68 }));
  box.position.set(cx, cy, cz); scene.add(box);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(W, D), new THREE.MeshBasicMaterial({ color: 0x1b2229, transparent: true, opacity: 0.8, side: THREE.DoubleSide }));
  floor.rotation.x = -Math.PI / 2; floor.position.set(cx, R.z[0], cz); scene.add(floor);
  const grid = new THREE.GridHelper(Math.max(W, D), Math.round(Math.max(W, D) / 0.3048), 0x2b343d, 0x232c35); grid.position.set(cx, R.z[0] + 0.002, cz); scene.add(grid);
  const fl = labelSprite('FRONT', '#94a1ad'); fl.position.set(cx, R.z[0] + 0.1, -R.y[1] + 0.2); scene.add(fl);
  const bl = labelSprite('BACK', '#94a1ad'); bl.position.set(cx, R.z[0] + 0.1, -R.y[0] - 0.2); scene.add(bl);

  seatGroup = new THREE.Group();
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.1, 20, 14), new THREE.MeshStandardMaterial({ color: 0x94a1ad }));
  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.2, -R.z[0] - 0.12, 16), new THREE.MeshStandardMaterial({ color: 0x3a4651, transparent: true, opacity: 0.6 }));
  body.position.y = (R.z[0] - 0.12) / 2;
  seatGroup.add(head, body); scene.add(seatGroup);

  for (const s of L.speakers) {
    if (!s.pos) continue;
    const heights = s.role === 'height';
    const col = heights ? 0xc4a7ff : 0x7fb7e6;
    const m = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.28, 0.18), new THREE.MeshStandardMaterial({ color: 0x2b343d, emissive: col, emissiveIntensity: 0.05 }));
    m.position.copy(tv(s.pos)); m.lookAt(0, 0, 0);
    scene.add(m);
    const lab = labelSprite(s.name, heights ? '#c4a7ff' : '#7fb7e6'); lab.position.copy(m.position).add(new THREE.Vector3(0, 0.25, 0)); scene.add(lab);
    spkMeshes[s.name] = { mesh: m, port: s.port };
    const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), tv(s.pos)]);
    const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0 }));
    scene.add(line); rays.push({ line, port: s.port });
  }

  objMesh = new THREE.Mesh(new THREE.SphereGeometry(0.11, 24, 16), new THREE.MeshStandardMaterial({ color: 0xffc15a, emissive: 0xffa020, emissiveIntensity: 0.6 }));
  scene.add(objMesh);
  hitMesh = new THREE.Mesh(new THREE.SphereGeometry(0.3), new THREE.MeshBasicMaterial({ visible: false }));
  objMesh.add(hitMesh);
  dropLine = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, -1, 0)]), new THREE.LineBasicMaterial({ color: 0x8a6a2c }));
  scene.add(dropLine);
  floorMark = new THREE.Mesh(new THREE.RingGeometry(0.1, 0.14, 24), new THREE.MeshBasicMaterial({ color: 0xffc15a, side: THREE.DoubleSide }));
  floorMark.rotation.x = -Math.PI / 2; scene.add(floorMark);
  trail = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xffc15a, transparent: true, opacity: 0.4 }));
  scene.add(trail);

  canvas.addEventListener('pointerdown', onDown);
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerup', onUp);
  canvas.addEventListener('pointercancel', onUp);
  new ResizeObserver(resize).observe($('stage'));
  resize(); camera('overview');
  requestAnimationFrame(frame);
}

function resize() {
  const st = $('stage'), w = st.clientWidth, h = st.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false); camera3.aspect = w / h; camera3.updateProjectionMatrix();
}

function camera(name) {
  camName = name;
  const R = L.room, W = R.width_m, D = R.depth_m, H = R.height_m;
  const c = new THREE.Vector3((R.x[0] + R.x[1]) / 2, (R.z[0] + R.z[1]) / 2, -(R.y[0] + R.y[1]) / 2);
  const big = Math.max(W, D, H);
  seatGroup.visible = name !== 'seat';
  if (name === 'overview') { controls.target.copy(c); camera3.position.copy(c).add(new THREE.Vector3(W * 0.9, H * 0.8, D * 1.1)); }
  else if (name === 'top') { controls.target.copy(c); camera3.position.copy(c).add(new THREE.Vector3(0, big * 1.7, 0.001)); }
  else if (name === 'side') { controls.target.copy(c); camera3.position.copy(c).add(new THREE.Vector3(big * 1.6, 0, 0.001)); }
  else { controls.target.set(0, 0.05, -0.1); camera3.position.set(0, 0.05, 0.0); }
  controls.update();
  document.querySelectorAll('[data-cam]').forEach((b) => b.classList.toggle('on', b.dataset.cam === name));
}

function frame() {
  requestAnimationFrame(frame);
  if (S) {
    const target = tv(S.pos);
    objMesh.position.lerp(target, dragging ? 0.6 : 0.4);
    const p = objMesh.position, floorY = L.room.z[0];
    dropLine.position.copy(p); dropLine.scale.y = Math.max(p.y - floorY, 0.001);
    floorMark.position.set(p.x, floorY + 0.004, p.z);
    const m = S.meters, live = !S.muted ? 1 : 0.15;
    for (const r of rays) {
      const g = Math.min(1, Math.abs(m.gains[r.port] || 0)) * live;
      r.line.material.opacity = g * 0.9;
      const pos = r.line.geometry.attributes.position; pos.setXYZ(0, p.x, p.y, p.z); pos.needsUpdate = true;
    }
    for (const k in spkMeshes) {
      const sm = spkMeshes[k]; const g = Math.min(1, Math.abs(m.gains[sm.port] || 0)) * live;
      sm.mesh.material.emissiveIntensity = 0.05 + g * 1.4;
    }
    objMesh.material.emissiveIntensity = S.muted ? 0.15 : 0.6 + 0.4 * S.gate;
    const last = trailPts[trailPts.length - 1];
    if (!last || last.distanceToSquared(p) > 0.0004) {
      trailPts.push(p.clone()); if (trailPts.length > 160) trailPts.shift();
      trail.geometry.setFromPoints(trailPts);
    }
  }
  controls.update();
  renderer.render(scene, camera3);
}

// ---- dragging

const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
function setRay(e) {
  const r = $('view').getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera3);
}
function onDown(e) {
  if (!S) return;
  setRay(e);
  if (!ray.intersectObject(hitMesh).length) return;
  dragging = true; vertDrag = e.shiftKey || $('vertMode').checked;
  controls.enabled = false; $('view').setPointerCapture(e.pointerId);
  const p = objMesh.position;
  if (vertDrag) {
    const n = new THREE.Vector3(camera3.position.x - p.x, 0, camera3.position.z - p.z);
    if (n.lengthSq() < 1e-6) n.set(0, 0, 1);
    dragPlane.setFromNormalAndCoplanarPoint(n.normalize(), p);
  } else dragPlane.setFromNormalAndCoplanarPoint(new THREE.Vector3(0, 1, 0), p);
  const hit = new THREE.Vector3();
  if (ray.ray.intersectPlane(dragPlane, hit)) dragOffset.copy(hit).sub(p); else dragOffset.set(0, 0, 0);
  e.preventDefault();
}
function onMove(e) {
  setRay(e);
  if (!dragging) { $('view').style.cursor = ray.intersectObject(hitMesh).length ? 'grab' : 'default'; return; }
  const hit = new THREE.Vector3();
  if (!ray.ray.intersectPlane(dragPlane, hit)) return;
  hit.sub(dragOffset);
  const cur = tv(S.pos);
  let np = vertDrag ? new THREE.Vector3(cur.x, hit.y, cur.z) : new THREE.Vector3(hit.x, cur.y, hit.z);
  const R = L.room, q = fromV(np);
  q[0] = clamp(q[0], R.x[0], R.x[1]); q[1] = clamp(q[1], R.y[0], R.y[1]); q[2] = clamp(q[2], R.z[0], R.z[1]);
  if (now() - lastPosSent > 30) { lastPosSent = now(); send({ t: 'pos', p: q }); }
  $('view').style.cursor = 'grabbing';
}
function onUp(e) {
  if (!dragging) return;
  dragging = false; controls.enabled = true;
  try { $('view').releasePointerCapture(e.pointerId); } catch { /* already released */ }
}

// ------------------------------------------------------------------ keyboard

document.addEventListener('keydown', (e) => {
  const tg = e.target, tag = tg && tg.tagName;
  const typing = tag === 'TEXTAREA' || tag === 'SELECT' || (tag === 'INPUT' && tg.type !== 'range' && tg.type !== 'checkbox');
  if (e.key === 'Escape') { panic(); e.preventDefault(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey || !S) return;
  const onRange = tag === 'INPUT' && tg.type === 'range';
  const step = e.shiftKey ? 0.5 : 0.1;
  switch (e.key) {
    case 'm': case 'M': toggleMute(); break;
    case 'v': case 'V': $('vertMode').checked = !$('vertMode').checked; break;
    case '1': camera('overview'); break; case '2': camera('top'); break; case '3': camera('side'); break; case '4': camera('seat'); break;
    case 'i': case 'I': queue({ mode: S.params.mode === 'identify' ? 'object' : 'identify' }); break;
    case ' ': if (tag === 'BUTTON' || onRange) return; if (S.motion) cmd('stop_motion'); else playPath(); break;
    case 'ArrowLeft': if (onRange) return; nudge('-x', step); break;
    case 'ArrowRight': if (onRange) return; nudge('+x', step); break;
    case 'ArrowUp': if (onRange) return; nudge('+y', step); break;
    case 'ArrowDown': if (onRange) return; nudge('-y', step); break;
    case 'PageUp': nudge('+z', step); break;
    case 'PageDown': nudge('-z', step); break;
    default: return;
  }
  e.preventDefault();
});

connect();
