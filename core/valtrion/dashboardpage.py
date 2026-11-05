"""The page of the dashboard: one HTML document, no external files, no network requests but to its own server.

Every value that comes from the machine (a purpose, a path, a command line) is put on the page with `textContent`, never as HTML."""
from __future__ import annotations

from . import brandassets

STYLE = """
:root{--bg:#101f34;--surface:#14283f;--surface-2:#183049;--line:#23405f;--text:#f5f8fc;--muted:#8aa0bb;--accent:#1689f8;
--blue:#00c4ff;--teal:#00e2ca;--teal-deep:#00beb5;--red:#ff6b6b;--amber:#f5b84c;--glow:#00c4ff33;
--font:Outfit,Inter,"Segoe UI",system-ui,sans-serif;--body:Inter,"Segoe UI",system-ui,sans-serif}
*{box-sizing:border-box}html{scroll-behavior:smooth}html,body{margin:0;background:var(--bg);color:var(--text);font-family:var(--body);font-size:14px}
body{min-height:100vh;background:radial-gradient(1100px 520px at 88% -10%,#0d3a5c66,transparent 70%),var(--bg)}
header{display:flex;align-items:center;gap:14px;padding:18px 28px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header .trident{width:34px;height:34px}
h1{font:600 24px/1 var(--font);letter-spacing:-.02em;margin:0}
.hosts{display:flex;gap:8px;flex-wrap:wrap}
.hostchip{display:flex;align-items:center;gap:8px;font:inherit;font-size:13px;color:var(--muted);background:var(--surface);border:1px solid var(--line);
border-radius:999px;padding:5px 12px;cursor:pointer}
.hostchip:hover{border-color:#1689f8aa;color:var(--text)}.hostchip.sel{border-color:var(--accent);color:var(--text);box-shadow:0 0 0 3px #1689f822}
.hostchip .dot{width:7px;height:7px;box-shadow:none}.hostchip .dot.wait{background:var(--muted)}.hostchip .dot.bad{background:var(--red)}
.badge{background:var(--accent);color:#fff;border-radius:99px;font-size:11px;font-weight:600;padding:0 7px;line-height:17px}
.spacer{flex:1}.live{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--teal);box-shadow:0 0 0 4px var(--glow)}.dot.off{background:var(--red);box-shadow:none}
main{padding:22px 28px 48px;max-width:1280px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:26px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.tile .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.tile .value{font:600 30px/1.2 var(--font);margin-top:4px}.tile .value small{display:block;font:400 13px/1.3 var(--body);color:var(--muted);margin-top:1px}
.tile .note{color:var(--muted);font-size:12px;margin-top:2px}.tile.warn .value{color:var(--amber)}
a.tile{display:block;color:inherit;text-decoration:none;cursor:pointer;transition:border-color .15s,transform .15s}
a.tile:hover{border-color:var(--accent);transform:translateY(-1px)}a.tile:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tile .meter{height:5px;border-radius:99px;background:#0c1a2c;overflow:hidden;margin-top:10px}
.tile .meter i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--teal));border-radius:99px}
.tile.warn .meter i{background:var(--amber)}
h2,h3{scroll-margin-top:14px}
@keyframes flash{0%{color:var(--blue)}100%{color:inherit}}h2:target{animation:flash 1.6s ease-out}
.card+.card{margin-top:12px}
h2{font:600 16px/1 var(--font);margin:28px 0 6px;display:flex;align-items:center;gap:10px}
h2 .count{color:var(--muted);font-weight:400;font-size:13px}
.hint{color:var(--muted);font-size:12.5px;margin:0 0 12px;max-width:820px;line-height:1.5}
h3{font:500 12px/1 var(--body);text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:16px 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:13px 15px;min-height:104px}
.card.active{border-color:#1689f8aa;box-shadow:0 0 0 1px #1689f833,0 8px 28px #00c4ff12}
.card.free{background:transparent;border-style:dashed;opacity:.75}.card.off{opacity:.45}
.card .head{display:flex;justify-content:space-between;align-items:center;gap:8px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.card .title{font:600 15px/1.3 var(--font);overflow-wrap:anywhere;margin-top:6px}.card .sub{color:var(--muted);font-size:12px;margin-top:3px;overflow-wrap:anywhere}
.steps{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}
.pill{border-radius:999px;padding:2px 9px;font-size:12px;border:1px solid var(--line);color:var(--muted)}
.pill.running{border-color:var(--blue);color:var(--blue)}.pill.PASS{border-color:var(--teal-deep);color:var(--teal)}
.pill.FAIL,.pill.TIMEOUT,.pill.STALL{border-color:var(--red);color:var(--red)}.pill.BLOCKED{border-color:var(--amber);color:var(--amber)}
.bar{height:6px;border-radius:99px;background:#0c1a2c;overflow:hidden;margin-top:10px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--teal));border-radius:99px;transition:width .6s}
.bar.busy>i{width:40%;animation:slide 1.6s ease-in-out infinite}
.bar.live>i{position:relative;overflow:hidden}
.bar.live>i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,#ffffff77,transparent);transform:translateX(-100%);animation:shine 1.3s linear infinite}
@keyframes shine{to{transform:translateX(100%)}}
.bar.silent>i{background:var(--amber)}
.signal{display:flex;align-items:center;color:var(--muted);font-size:12px;margin-top:6px}.signal .warn{color:var(--amber)}
.dotlive{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--teal);margin-right:7px;animation:pulse 1.3s ease-in-out infinite}
.dotlive.quiet{background:var(--muted);animation:none}.dotlive.silent{background:var(--amber);animation:none}
@keyframes pulse{50%{opacity:.25}}
.session{display:inline-block;margin-top:5px;padding:1px 9px;border-radius:99px;background:#0c1a2c;border:1px solid var(--line);color:var(--blue);font-size:12px;overflow-wrap:anywhere}
@keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
.meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:6px;gap:12px}
.stack{display:flex;height:22px;border-radius:8px;overflow:hidden;background:#0c1a2c;margin:12px 0 10px}
.stack i{display:block;height:100%;min-width:2px;border-right:1px solid var(--surface)}
.legend{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 18px}
.legend div{display:flex;align-items:center;gap:8px;font-size:13px;min-width:0}
.legend .sw{width:10px;height:10px;border-radius:3px;flex:none}.legend .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend .num{color:var(--muted);font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.05em;background:var(--surface-2)}
tr:last-child td{border-bottom:0}
.status{font-weight:600;font-size:12px;border-radius:6px;padding:2px 8px;background:#0c1a2c;white-space:nowrap;display:inline-block}
.status.PASS{color:var(--teal)}.status.FAIL,.status.TIMEOUT,.status.STALL,.status.offline{color:var(--red)}
.status.BLOCKED,.status.NEEDS_PERMISSION{color:var(--amber)}
.status.held,.status.running,.status.busy{color:var(--blue)}.status.free{color:var(--muted)}.status.stale{color:var(--amber)}
.empty{color:var(--muted);padding:10px 2px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:10px}
"""

SCRIPT = r"""
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
const dur = (s) => { if (s == null) return '–'; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : m ? `${m}m ${String(x).padStart(2, '0')}s` : `${x}s`; };
const gb = (mib) => (mib / 1024).toFixed(mib >= 10240 ? 0 : 1) + ' GB';
const short = (s, n = 70) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '');
const baseName = (p) => (p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop();
const COLORS = ['#00c4ff', '#00e2ca', '#1689f8', '#7c8cff', '#f5b84c', '#ff8f6b', '#b58cff', '#5fd1a0'];
let last = 0, all = null, sel = 'local';

function fill(node, children) { node.replaceChildren(...children); }
function tile(label, value, note, warn, tip, small, href, used) {
  const t = el(href ? 'a' : 'div', 'tile' + (warn ? ' warn' : '')); if (tip) t.title = tip; if (href) t.href = href;
  const v = el('div', 'value', value); if (small) v.append(el('small', '', small));
  t.append(el('div', 'label', label), v, el('div', 'note', note || ''));
  if (used != null) { const m = el('div', 'meter'), i = el('i'); i.style.width = Math.max(0, Math.min(100, Math.round(100 * used))) + '%'; m.append(i); t.append(m); }
  return t;
}
function status(text) { return el('span', 'status ' + text, text); }
function table(heads, rows) {
  const t = el('table'), head = el('tr'); heads.forEach((h) => head.append(el('th', '', h))); t.append(head);
  rows.forEach((r) => { const tr = el('tr'); r.forEach((c) => { const td = el('td'); if (c instanceof Node) td.append(c); else td.textContent = c == null ? '' : c; tr.append(td); }); t.append(tr); });
  return t;
}
function busyBar() { const b = el('div', 'bar busy'); b.append(el('i')); return b; }
// How alive a run looks: a signal (the Editor's progress file or the app's heartbeat) newer than 15 s is "live", older than 60 s is "silent".
function aliveness(ageSec) { return ageSec == null ? 'quiet' : ageSec <= 15 ? 'live' : ageSec <= 60 ? 'quiet' : 'silent'; }
function doneBar(fraction, ageSec) {
  const b = el('div', 'bar ' + aliveness(ageSec)), i = el('i'); i.style.width = Math.max(1, Math.min(100, Math.round(100 * fraction))) + '%'; b.append(i); return b;
}
function testsText(p) { return `${p.done} of ${p.total} tests` + (p.failed ? ` · ${p.failed} failed` : ''); }
function etaText(p) {
  const a = Date.parse(p.startedUtc), z = Date.parse(p.utc);
  if (!(p.done >= 3) || isNaN(a) || isNaN(z) || z <= a) return '';
  return '~' + dur((p.total - p.done) * (z - a) / 1000 / p.done) + ' left';
}
function signal(ageSec, what, busySec) {
  const s = el('div', 'signal'), state = aliveness(ageSec);
  // The Editor writes from a background thread, so a long synchronous test keeps the signal alive: say that the main thread is busy with it.
  const busy = state === 'live' && busySec != null && busySec > 20 ? ` · busy with one test for ${dur(busySec)}` : '';
  s.append(el('span', 'dotlive ' + state), el('span', state === 'silent' ? 'warn' : '', ageSec == null ? '' : state === 'silent' ? `no signal from ${what} for ${dur(ageSec)}` : state === 'live' ? `live · updated ${Math.round(ageSec)}s ago${busy}` : `last signal ${dur(ageSec)} ago`));
  return s;
}
function sessionChip(name) { return name ? el('div', 'session', name) : null; }
function emptyBar() { return el('div', 'bar'); }

function slotCard(title, state, head) {
  const c = el('div', 'card ' + state);
  const h = el('div', 'head'); h.append(el('span', '', head), status(state === 'active' ? 'busy' : state === 'off' ? 'offline' : 'free')); c.append(h);
  c.append(el('div', 'title', title)); return c;
}
function editorSlot(s, total) {
  if (s.state !== 'held') { const c = slotCard('Free', 'free', `Unity Editor · slot ${s.slot} of ${total}`); c.append(emptyBar()); return c; }
  const c = slotCard(s.purpose || 'unity run', 'active', `Unity Editor · slot ${s.slot} of ${total}`);
  const p = s.progress && s.progress.total > 0 ? s.progress : null, chip = sessionChip(s.worktree);
  if (chip) c.append(chip);
  c.append(el('div', 'sub', [baseName(s.project), s.owner, s.pid ? `pid ${s.pid}` : ''].filter(Boolean).join(' · ')));
  if (p && p.current) c.append(el('div', 'sub', short(p.current, 90)));
  c.append(p ? doneBar(p.done / p.total, p.updatedSec) : busyBar());
  const m = el('div', 'meta');
  if (p) m.append(el('span', '', testsText(p)), el('span', '', [`${Math.round(100 * p.done / p.total)}%`, etaText(p), dur(s.heldSec)].filter(Boolean).join(' · ')));
  else m.append(el('span', '', `running ${dur(s.heldSec)}`), el('span', '', `alive signal ${dur(s.renewedSec)} ago`));
  c.append(m);
  if (p) c.append(signal(p.updatedSec, 'Unity', p.mainSec));
  return c;
}
function deviceSlot(d) {
  const c = slotCard(d.model || d.id, !d.online ? 'off' : (d.busy ? 'active' : 'free'), `${d.platform || 'device'} · 1 slot`);
  c.append(el('div', 'sub', [d.os, d.kind === 'emulator' ? 'emulator' : ''].filter(Boolean).join(' · ')));
  if (d.busy) { c.append(busyBar()); const m = el('div', 'meta'); m.append(el('span', '', short(d.purpose || '', 60)), el('span', '', dur(d.heldSec))); c.append(m); } else c.append(emptyBar());
  return c;
}
function jobCard(j) {
  const c = el('div', 'card active');
  c.append(el('div', 'title', `${j.project || j.kind || 'job'} · ${j.kind || ''}`));
  const chip = sessionChip(j.worktree); if (chip) c.append(chip);
  c.append(el('div', 'sub', j.id));
  const steps = el('div', 'steps'); (j.steps || []).forEach((s) => steps.append(el('span', 'pill ' + s.status, `${s.step}  ${s.status === 'running' ? '…' : s.status}`))); c.append(steps);
  const d = j.device;
  c.append(el('div', 'sub', j.phase ? `phase: ${j.phase}` : (j.message || '')));
  if (d && d.step > 0 && d.total) c.append(el('div', 'sub', `on the device: step ${d.step} of ${d.total}${d.stepType ? ' · ' + d.stepType : ''}`), doneBar(d.step / d.total, d.ageSec), signal(d.ageSec, 'the app'));
  else if (d && d.step > 0) c.append(el('div', 'sub', `on the device: step ${d.step}${d.stepType ? ' · ' + d.stepType : ''}`), busyBar());
  else c.append(busyBar());
  const stall = j.stallSec ? Math.min(100, Math.round(100 * (j.lastProgressSec || 0) / j.stallSec)) : 0;
  const m = el('div', 'meta'); m.append(el('span', '', `running ${dur(j.ageSec)}`), el('span', '', `last progress ${dur(j.lastProgressSec)} ago` + (j.stallSec ? ` · stall at ${dur(j.stallSec)} (${stall}%)` : ''))); c.append(m);
  return c;
}
function memoryCard(m, mach) {
  const c = el('div', 'card'); c.style.minHeight = '0';
  if (!m) { c.append(el('div', 'sub', mach.freeMemoryMiB == null ? 'Memory is not available on this host.' : `Free memory: ${gb(mach.freeMemoryMiB)} (the host's Valtrion cannot show the breakdown yet).`)); return c; }
  const used = m.usedMiB == null ? null : m.usedMiB;
  c.append(el('div', 'title', `${used == null ? '' : gb(used) + ' in use of '}${gb(m.totalMiB)}${m.freeMiB == null ? '' : ' · ' + gb(m.freeMiB) + ' free'}`));
  const stack = el('div', 'stack'); const legend = el('div', 'legend');
  m.groups.forEach((g, i) => {
    const color = g.other ? '#5d7694' : COLORS[i % COLORS.length], part = el('i'); part.style.width = (100 * g.mib / m.totalMiB) + '%'; part.style.background = color; part.title = `${g.label}: ${gb(g.mib)}`; stack.append(part);
    const row = el('div'), sw = el('span', 'sw'); sw.style.background = color;
    row.append(sw, el('span', 'name', g.label + (g.count > 1 ? ` ×${g.count}` : '')), el('span', 'num', `${gb(g.mib)} · ${(100 * g.mib / m.totalMiB).toFixed(1)}%`)); legend.append(row);
  });
  c.append(stack, legend); return c;
}
function diskCard(mach) {
  const c = el('div', 'card'); c.style.minHeight = '0';
  if (mach.diskTotalGiB == null) { c.append(el('div', 'sub', 'Disk space is not available on this host.')); return c; }
  const used = mach.diskTotalGiB - mach.diskFreeGiB;
  c.append(el('div', 'title', `Disk: ${Math.round(mach.diskFreeGiB)} GB free of ${Math.round(mach.diskTotalGiB)} GB · ${Math.round(used)} GB in use`));
  const stack = el('div', 'stack'), part = el('i'); part.style.width = (100 * used / mach.diskTotalGiB) + '%'; part.style.background = '#1689f8'; stack.append(part); c.append(stack);
  return c;
}
function renderView(s) {
  const slots = s.slots || { capacity: 0, unityEditor: s.leases.filter((l) => l.resource.startsWith('unity-editor:')).map((l, i) => ({ slot: i + 1, state: l.state, purpose: l.purpose, owner: l.owner, pid: l.pid, heldSec: l.heldSec, renewedSec: l.renewedSec })) };
  const total = slots.capacity || slots.unityEditor.length, busy = slots.unityEditor.filter((x) => x.state === 'held').length;
  const devs = (s.devices || []).filter((d) => d.exclusive), devBusy = devs.filter((d) => d.busy).length;
  const active = s.jobs.filter((j) => j.active), waiting = s.leases.reduce((n, l) => n + (l.waiting || 0), 0);
  const editors = s.unity.editors.filter((e) => !e.worker), workers = s.unity.editors.length - editors.length;
  const mach = s.machine, mem = mach.memory, free = mach.freeMemoryMiB, low = free != null && free < 2048;
  const diskUsed = mach.diskTotalGiB ? 1 - mach.diskFreeGiB / mach.diskTotalGiB : null;
  fill($('tiles'), [
    tile('Unity slots', `${total - busy}`, `${busy} busy`, total > 0 && busy === total, 'Free places for a Unity Editor on this host (it allows a fixed number at once). Click to see them.', `free of ${total}`, '#sec-slots', total ? busy / total : 0),
    tile('Device slots', `${devs.length - devBusy}`, devs.length ? `${devBusy} busy` : 'no devices registered', false, 'Free phones, tablets and emulators: each takes one run at a time. Click to see them.', `free of ${devs.length}`, '#sec-slots', devs.length ? devBusy / devs.length : 0),
    tile('Jobs running', active.length, 'background pipelines', false, 'Detached jobs started with `valtrion pipeline` or `job start`, such as device tests. Click to see them.', null, '#sec-jobs-running'),
    tile('Waiting in line', waiting, waiting ? 'runs waiting for a slot, project or device' : 'no run is waiting', waiting > 0, 'Runs that asked for a slot, a Unity project or a device that somebody else holds. Click to see the leases.', null, '#sec-leases'),
    tile('Memory', free == null ? '–' : gb(free), low ? 'low: new Unity runs wait' : (mem && mem.usedMiB != null ? `${gb(mem.usedMiB)} in use` : ''), low,
      'Free memory. Below 2 GB a new Unity run waits for memory. Click to see who holds it.', mem ? `free of ${gb(mem.totalMiB)}` : 'free', '#sec-memory', mem && mem.usedMiB != null ? mem.usedMiB / mem.totalMiB : null),
    tile('Disk', mach.diskFreeGiB == null ? '–' : Math.round(mach.diskFreeGiB) + ' GB', mach.diskTotalGiB ? `${Math.round(mach.diskTotalGiB - mach.diskFreeGiB)} GB in use` : '', false,
      'Free space on the disk with the Valtrion folder. Click to see it.', mach.diskTotalGiB ? `free of ${Math.round(mach.diskTotalGiB)} GB` : 'free', '#sec-memory', diskUsed)]);

  $('slots-count').textContent = `${busy + devBusy} busy of ${total + devs.length}`;
  const cards = [el('h3', '', `Unity Editor · ${total} slot${total === 1 ? '' : 's'}`), el('div', 'grid')];
  slots.unityEditor.forEach((x) => cards[1].append(editorSlot(x, total)));
  if (devs.length) { const dh = el('h3', '', 'Devices · one slot each'); cards.push(dh); const g = el('div', 'grid'); devs.forEach((d) => g.append(deviceSlot(d))); cards.push(g); }
  fill($('slots'), cards);

  $('jobs-running-count').textContent = active.length || '';
  fill($('jobs-running'), active.length ? [Object.assign(el('div', 'grid'), {})] : [el('div', 'empty', 'No background job is running.')]);
  if (active.length) { const g = $('jobs-running').firstChild; active.forEach((j) => g.append(jobCard(j))); }

  fill($('memory'), [memoryCard(mem, mach), diskCard(mach)]);
  $('lease-count').textContent = s.leases.filter((l) => l.state === 'held').length;
  fill($('leases'), [table(['Resource', 'State', 'Held by', 'Purpose', 'Held for', 'Waiting'], s.leases.map((l) => [l.resource, status(l.state), l.owner ? `${l.owner} (pid ${l.pid})` : '', short(l.purpose || baseName(l.worktree), 90), dur(l.heldSec), l.waiting || '']))]);
  $('unity-count').textContent = editors.length + (workers ? ` + ${workers} workers` : '');
  fill($('unity'), [s.unity.readable ? (editors.length ? table(['PID', 'Project', 'Mode'], editors.map((e) => [e.pid, short(baseName(e.project) || e.project, 60), [e.batch ? 'batch' : 'interactive', e.tests ? 'tests' : ''].filter(Boolean).join(' · ')])) : el('div', 'empty', 'No Unity Editor is running.')) : el('div', 'empty', 'The process list could not be read.')]);
  $('jobs-count').textContent = s.jobs.length;
  fill($('jobs'), [table(['Job', 'Project', 'Status', 'Started', 'Reason'], s.jobs.map((j) => [short(j.id, 56), j.project, status(j.active ? 'running' : (j.status || j.state || '')), j.ageSec == null ? '' : dur(j.ageSec) + ' ago', short(j.reason, 80)]))]);
}
function hostsOf(s) {
  const list = [{ id: 'local', name: s.host.id, os: s.host.os, snap: s }];
  (s.remotes || []).forEach((r) => list.push({ id: r.id, name: r.id, os: r.os, snap: r.snapshot || null, ok: r.ok, legacy: r.legacy, note: r.note, error: r.error, loading: r.loading, age: r.ageSec }));
  return list;
}
function busyCount(snap) {
  const slots = snap.slots ? snap.slots.unityEditor.filter((x) => x.state === 'held').length : snap.leases.filter((l) => l.resource.startsWith('unity-editor:') && l.state === 'held').length;
  return snap.jobs.filter((j) => j.active).length + slots;
}
function renderHosts(list) {
  fill($('hosts'), list.map((h) => {
    const b = el('button', 'hostchip' + (h.id === sel ? ' sel' : ''));
    const state = h.snap ? 'ok' : (h.loading ? 'wait' : (h.ok === false ? 'bad' : 'wait'));
    b.append(el('span', 'dot ' + state), el('span', '', `${h.name} · ${h.os}`));
    if (h.snap && busyCount(h.snap)) b.append(el('span', 'badge', busyCount(h.snap)));
    b.title = h.age != null ? `updated ${Math.round(h.age)}s ago` : '';
    b.onclick = () => { sel = h.id; if (all) render(all); };
    return b;
  }));
}
function renderMessage(h) {
  const text = h.loading ? `Asking ${h.name}…` : (h.legacy ? h.note : (h.error ? `${h.name} did not answer: ${h.error}` : 'No data.'));
  ['tiles', 'slots', 'jobs-running', 'memory', 'leases', 'unity', 'jobs'].forEach((id) => fill($(id), []));
  ['slots-count', 'jobs-running-count', 'lease-count', 'unity-count', 'jobs-count'].forEach((id) => { $(id).textContent = ''; });
  fill($('slots'), [el('div', 'empty', text)]);
}
function render(s) {
  all = s; last = Date.now();
  const list = hostsOf(s);
  if (!list.some((h) => h.id === sel)) sel = 'local';
  renderHosts(list);
  const h = list.find((x) => x.id === sel);
  if (h.snap) renderView(h.snap); else renderMessage(h);
}
async function poll() {
  try {
    const r = await fetch('/api/snapshot', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    render(await r.json()); $('dot').className = 'dot';
  } catch (e) { $('dot').className = 'dot off'; }
}
setInterval(() => { const age = last ? Math.round((Date.now() - last) / 1000) : null; $('updated').textContent = age == null ? 'connecting…' : (age < 5 ? 'live' : `updated ${age}s ago`); }, 1000);
poll(); setInterval(poll, 2000);
"""


def page() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Valtrion</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" type="image/png" href="/favicon.png">
<style>{STYLE}</style></head><body>
<header>{brandassets.TRIDENT_SVG}<h1>Valtrion</h1><span class="hosts" id="hosts"></span><span class="spacer"></span>
<span class="live"><span class="dot off" id="dot"></span><span id="updated">connecting…</span></span></header>
<main>
<div class="tiles" id="tiles"></div>
<h2 id="sec-slots">Slots <span class="count" id="slots-count"></span></h2>
<p class="hint">A slot is a place for one run. The host allows a fixed number of Unity Editors at once, and each phone or tablet takes one run at a time. A slot is filled while a run uses it; the rest wait in line.</p>
<div id="slots"></div>
<h2 id="sec-jobs-running">Jobs running <span class="count" id="jobs-running-count"></span></h2><div id="jobs-running"></div>
<h2 id="sec-memory">Memory and disk</h2><p class="hint">What the machine has and who holds it: every Unity process is listed under the project it works on.</p><div id="memory"></div>
<h2 id="sec-leases">Leases <span class="count" id="lease-count"></span></h2>
<p class="hint">A lease is the lock a run holds on a resource so that nobody else uses it at the same time. A Unity run holds two: its project and one Editor slot. "Waiting" counts the runs queued for that resource.</p>
<div id="leases"></div>
<h2 id="sec-unity">Unity Editors <span class="count" id="unity-count"></span></h2><div id="unity"></div>
<h2 id="sec-jobs">Recent jobs <span class="count" id="jobs-count"></span></h2><div id="jobs"></div>
</main><footer>Valtrion dashboard · read-only</footer>
<script>{SCRIPT}</script></body></html>
"""
