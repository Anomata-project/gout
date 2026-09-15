// gout's window: the timeline drawn from the waveform levels gout sends, following the ui.
// Two canvases: "wave" is drawn again only when the view or the project changes, "over" (the
// playhead) every frame, so moving the playhead costs almost nothing.
"use strict";

const token = new URLSearchParams(location.search).get("t") || "";
const LABEL = 150;          // px for the names on the left
const RULER = 34;           // px for the time (and bars) on top
const MASTER_H = 64;
const TAKE_H = 56;
const GAP = 2;
const TAKE_POLL_MS = 100;   // a take's peaks come every 0.1 s
const MIN_MS_PER_PX = 0.02; // about one sample per px at 48 kHz, and closer
const DIM = 0.55;

const wave = document.getElementById("wave");
const over = document.getElementById("over");
const scroller = document.getElementById("scroller");
let state = null;
let version = -1;
const peaks = {};            // file -> {stamp, rate, block, levels: [Float32Array of lo,hi pairs]}
const pending = {};          // file -> stamp being fetched
const samples = {};          // file -> {from, to, rate, data: Float32Array interleaved stereo}
let samplesTimer = 0;
let view = { start: 0, msPerPx: 20, fitted: false };
let play = { pos: 0, playing: false, base: 0, baseAt: performance.now() };
let take = null;             // {at, label, peaks: []}
let follow = true;
let lanes = [];              // [{kind, y, h, track}] as last drawn
let needDraw = true;

function q(path, extra = "") { return `${path}?t=${encodeURIComponent(token)}${extra}`; }

function send(message) {
  fetch(q("/key"), { method: "POST", body: JSON.stringify(message) }).catch(() => {});
}

function fmt(ms) {
  const sign = ms < 0 ? "-" : "";
  ms = Math.abs(ms);
  const m = Math.floor(ms / 60000), s = Math.floor(ms / 1000) % 60, rest = Math.floor(ms % 1000);
  return `${sign}${m}:${String(s).padStart(2, "0")}.${String(rest).padStart(3, "0")}`;
}

function width() { return scroller.clientWidth; }
function toX(ms) { return LABEL + (ms - view.start) / view.msPerPx; }
function fromX(x) { return view.start + (x - LABEL) * view.msPerPx; }

async function loadState() {
  const reply = await fetch(q("/state"));
  if (!reply.ok) return;
  state = await reply.json();
  version = state.version;
  const files = state.tracks.map(t => [t.file, t.stamp]);
  if (state.master) files.push([state.master.file, state.master.stamp]);
  for (const [file, stamp] of files) {
    if ((!peaks[file] || peaks[file].stamp !== stamp) && pending[file] !== stamp) loadPeaks(file, stamp);
  }
  if (!view.fitted) fit();
  document.getElementById("song").textContent = state.name || "gout";
  needDraw = true;
}

async function loadPeaks(file, stamp) {
  pending[file] = stamp;
  const reply = await fetch(q("/peaks", `&file=${encodeURIComponent(file)}`));
  delete pending[file];
  if (!reply.ok) return;
  const buf = await reply.arrayBuffer();
  const head = new Uint32Array(buf, 0, 3);
  const [rate, block, count] = head;
  const sizes = new Uint32Array(buf, 12, count);
  const levels = [];
  let at = 12 + 4 * count;
  for (const pairs of sizes) {
    levels.push(new Float32Array(buf.slice(at, at + pairs * 8)));
    at += pairs * 8;
  }
  peaks[file] = { stamp, rate, block, levels };
  delete samples[file];
  needDraw = true;
}

function fit() {
  if (!state) return;
  const end = Math.max(state.length_ms, take ? take.at + take.peaks.length * TAKE_POLL_MS : 0, 1000);
  view.start = Math.min(0, ...state.tracks.map(t => t.offset_ms), 0);
  view.msPerPx = Math.max(MIN_MS_PER_PX, (end - view.start + 2000) / Math.max(100, width() - LABEL - 10));
  view.fitted = true;
  needDraw = true;
}

function zoomAt(x, factor) {
  const at = fromX(x);
  view.msPerPx = Math.min(Math.max(MIN_MS_PER_PX, view.msPerPx * factor), 3600000 / 100);
  view.start = at - (x - LABEL) * view.msPerPx;
  needDraw = true;
}

// ---- the waveform canvas

function layout() {
  const rows = [];
  let y = RULER;
  if (state.master) { rows.push({ kind: "master", y, h: MASTER_H }); y += MASTER_H + GAP; }
  const free = scroller.clientHeight - y - (take ? TAKE_H + GAP : 0);
  const h = Math.max(48, Math.min(140, Math.floor(free / Math.max(1, state.tracks.length)) - GAP));
  for (const t of state.tracks) { rows.push({ kind: "track", y, h, track: t }); y += h + GAP; }
  if (take) { rows.push({ kind: "take", y, h: TAKE_H }); y += TAKE_H + GAP; }
  return { rows, height: y };
}

function sizeCanvas(canvas, w, h) {
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

function draw() {
  if (!state) return;
  const w = width();
  const { rows, height } = layout();
  lanes = rows;
  const h = Math.max(height, scroller.clientHeight);
  document.getElementById("stack").style.height = `${h}px`;
  const ctx = sizeCanvas(wave, w, h);
  sizeCanvas(over, w, h);
  const c = state.colours;
  ctx.fillStyle = "#181818";
  ctx.fillRect(0, 0, w, h);
  drawRuler(ctx, w, h, c);
  let lane = 0;
  for (const row of rows) {
    ctx.fillStyle = lane++ % 2 ? "#1e1e1e" : "#222222";
    ctx.fillRect(0, row.y, w, row.h);
    ctx.save();
    ctx.beginPath();
    ctx.rect(LABEL, row.y, w - LABEL, row.h);
    ctx.clip();
    ctx.fillStyle = c.center_line;
    ctx.fillRect(LABEL, row.y + row.h / 2, w - LABEL, 1);
    if (row.kind === "master") drawMaster(ctx, row, w, c);
    else if (row.kind === "track") drawTrack(ctx, row, w, c);
    else drawTake(ctx, row, w, c);
    ctx.restore();
    drawLabel(ctx, row, c);
  }
  ctx.fillStyle = c.gap_line;
  ctx.fillRect(LABEL - 1, 0, 1, h);
  const detail = view.msPerPx < 1000 * (peaks[state.tracks[0]?.file]?.block || 256) / state.rate;
  document.getElementById("zoom").textContent =
    view.msPerPx >= 1 ? `1 px = ${view.msPerPx.toFixed(view.msPerPx < 10 ? 1 : 0)} ms` :
      `1 px = ${(view.msPerPx * state.rate / 1000).toFixed(2)} samples`;
  if (detail) askSamples();
  needDraw = false;
}

function drawRuler(ctx, w, h, c) {
  const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];
  const step = steps.find(s => s / view.msPerPx >= 90) || steps[steps.length - 1];
  ctx.font = "11px ui-monospace, monospace";
  ctx.textBaseline = "top";
  let t = Math.ceil(fromX(LABEL) / step) * step;
  for (; toX(t) < w; t += step) {
    const x = Math.round(toX(t)) + 0.5;
    ctx.fillStyle = c.ruler;
    ctx.fillRect(x, RULER - 10, 1, 10);
    ctx.fillStyle = "#262626";
    ctx.fillRect(x, RULER, 1, h - RULER);
    ctx.fillStyle = c.ruler_labels;
    const label = step >= 1000 ? fmt(t).replace(/\.000$/, "") : fmt(t);
    ctx.fillText(label, x + 3, 3);
  }
  if (state.bpm) {  // bars of four beats
    const beat = 60000 / state.bpm, bar = beat * 4;
    const every = Math.max(1, Math.ceil(40 * view.msPerPx / bar));
    let b = Math.max(0, Math.floor(fromX(LABEL) / bar));
    for (; toX(b * bar) < w; b += 1) {
      if (b % every) continue;
      const x = Math.round(toX(b * bar)) + 0.5;
      if (x < LABEL) continue;
      ctx.fillStyle = "#3a3a3a";
      ctx.fillRect(x, RULER - 18, 1, 8);
      ctx.fillStyle = c.ruler;
      ctx.fillText(String(b + 1), x + 3, 16);
    }
  }
  ctx.fillStyle = c.gap_line;
  ctx.fillRect(0, RULER - 1, w, 1);
}

function drawLabel(ctx, row, c) {
  ctx.fillStyle = "#141414";
  ctx.fillRect(0, row.y, LABEL - 1, row.h);
  ctx.font = "bold 12px ui-monospace, monospace";
  ctx.textBaseline = "top";
  if (row.kind === "master") {
    ctx.fillStyle = c.master_wave;
    ctx.fillText("master", 8, row.y + 6);
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillStyle = c.ruler_labels;
    ctx.fillText(state.master.current ? "master.wav" : "out of date", 8, row.y + 22);
    return;
  }
  if (row.kind === "take") {
    ctx.fillStyle = c.playhead;
    ctx.fillText(`● ${take.label}`, 8, row.y + 6);
    return;
  }
  const t = row.track;
  ctx.fillStyle = t.heard ? c.track_label : c.muted_wave;
  ctx.fillText(`${t.n} ${t.name}`.slice(0, 17), 8, row.y + 6);
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillStyle = c.ruler_labels;
  const bits = [];
  if (t.gain_db) bits.push(`${t.gain_db > 0 ? "+" : ""}${t.gain_db.toFixed(1)} dB`);
  if (Math.abs(t.pan) >= 0.005) bits.push(t.pan < 0 ? `L${Math.round(-t.pan * 100)}` : `R${Math.round(t.pan * 100)}`);
  if (t.mute) bits.push("M");
  if (t.solo) bits.push("S");
  if (bits.length) ctx.fillText(bits.join(" "), 8, row.y + 22);
  if (t.fx.length && row.h > 44) ctx.fillText(t.fx.join(" "), 8, row.y + 36);
}

// the lowest and highest sample in [t0, t1) of a file, from the finest level that is fine enough,
// or from the samples themselves when they are here
function span(file, t0, t1) {
  const s = samples[file];
  const rate = state.rate;
  if (s && peaks[file] && t0 >= s.from && t1 <= s.to && view.msPerPx < 1000 * peaks[file].block / rate) {
    let i0 = Math.max(0, Math.floor((t0 - s.from) * rate / 1000) * 2);
    let i1 = Math.min(s.data.length, Math.max(i0 + 2, Math.ceil((t1 - s.from) * rate / 1000) * 2));
    let lo = Infinity, hi = -Infinity;
    for (let i = i0; i < i1; i++) { const v = s.data[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    return lo === Infinity ? null : [lo, hi];
  }
  const p = peaks[file];
  if (!p) return null;
  let k = 0;
  while (k + 1 < p.levels.length && 1000 * p.block * 4 ** (k + 1) / rate <= view.msPerPx) k++;
  const msPer = 1000 * p.block * 4 ** k / rate;
  const level = p.levels[k];
  const i0 = Math.max(0, Math.floor(t0 / msPer));
  const i1 = Math.min(level.length / 2, Math.max(i0 + 1, Math.ceil(t1 / msPer)));
  if (i0 >= i1) return null;
  let lo = Infinity, hi = -Infinity;
  for (let i = i0; i < i1; i++) {
    const a = level[2 * i], b = level[2 * i + 1];
    if (a < lo) lo = a;
    if (b > hi) hi = b;
  }
  return [lo, hi];
}

// a file drawn where its start (zero) is on the timeline, only what lies in [inMs, outMs)
function drawRange(ctx, row, w, file, zero, inMs, outMs, colour, gainDb = 0) {
  const x0 = Math.max(LABEL, Math.floor(toX(zero + inMs)));
  const x1 = Math.min(w, Math.ceil(toX(zero + outMs)));
  if (x1 <= x0) return;
  const mid = row.y + row.h / 2, amp = (row.h / 2 - 3) * 10 ** (gainDb / 20);  // a part drawn at its own gain
  ctx.fillStyle = colour;
  for (let x = x0; x < x1; x++) {
    const t0 = Math.max(inMs, fromX(x) - zero), t1 = Math.min(outMs, fromX(x + 1) - zero);
    if (t1 <= t0) continue;
    const got = span(file, t0, t1);
    if (!got) continue;
    const half = row.h / 2 - 3;
    const top = Math.max(row.y, mid - Math.min(1, got[1]) * amp), bottom = Math.min(row.y + row.h, mid - Math.max(-1, got[0]) * amp);
    if (amp > half && (got[1] * amp > half || -got[0] * amp > half)) ctx.fillStyle = "#ff3030";  // louder than the lane
    else ctx.fillStyle = colour;
    ctx.fillRect(x, top, 1, Math.max(1, bottom - top));
  }
}

function drawMaster(ctx, row, w, c) {
  const m = state.master;
  if (!peaks[m.file]) return;
  ctx.globalAlpha = m.current ? 1 : DIM;
  drawRange(ctx, row, w, m.file, -m.head_ms, 0, m.length_ms, c.master_wave);
  ctx.globalAlpha = 1;
}

function drawTrack(ctx, row, w, c) {
  const t = row.track;
  if (!peaks[t.file]) {
    ctx.fillStyle = c.ruler_labels;
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText("reading the waveform…", LABEL + 10, row.y + row.h / 2 - 6);
    return;
  }
  const colour = c.track_palette[t.color % c.track_palette.length];
  if (!t.parts.length) {
    drawRange(ctx, row, w, t.file, t.offset_ms, 0, t.length_ms, c.trimmed_wave);  // what the soft trim leaves out
    drawRange(ctx, row, w, t.file, t.offset_ms, t.in_ms, t.out_ms, t.heard ? colour : c.muted_wave);
    return;
  }
  ctx.font = "11px ui-monospace, monospace";
  ctx.textBaseline = "top";
  for (const p of t.parts) {
    const zero = t.offset_ms + p.shift_ms;
    drawRange(ctx, row, w, t.file, zero, p.in_ms, p.out_ms, t.heard && !p.mute ? colour : c.muted_wave, p.gain_db);
    const x = Math.round(toX(zero + p.in_ms)) + 0.5;
    if (x >= LABEL && x < w) {
      ctx.fillStyle = c.ruler_labels;
      ctx.fillRect(x, row.y, 1, row.h);
      ctx.fillText(p.label + (p.fx.length ? ` ${p.fx.join(" ")}` : ""), x + 4, row.y + 3);
    }
  }
}

function drawTake(ctx, row, w, c) {
  const mid = row.y + row.h / 2, amp = row.h / 2 - 3;
  for (let x = LABEL; x < w; x++) {
    const i0 = Math.floor((fromX(x) - take.at) / TAKE_POLL_MS), i1 = Math.ceil((fromX(x + 1) - take.at) / TAKE_POLL_MS);
    if (i1 <= 0 || i0 >= take.peaks.length) continue;
    let top = 0;
    for (let i = Math.max(0, i0); i < Math.min(take.peaks.length, Math.max(i0 + 1, i1)); i++) top = Math.max(top, take.peaks[i]);
    ctx.fillStyle = top >= 0.999 ? "#ff3030" : c.master_wave;  // clipped
    ctx.fillRect(x, mid - top * amp, 1, Math.max(1, 2 * top * amp));
  }
}

function askSamples() {  // the samples of what is in view, a moment after the view stops changing
  clearTimeout(samplesTimer);
  samplesTimer = setTimeout(async () => {
    const t0 = fromX(LABEL), t1 = fromX(width());
    const wanted = [];  // [file, where its zero is on the timeline, its length]
    if (state.master) wanted.push([state.master.file, -state.master.head_ms, state.master.length_ms]);
    for (const t of state.tracks) {
      const zeros = t.parts.length ? t.parts.map(p => t.offset_ms + p.shift_ms) : [t.offset_ms];
      for (const zero of new Set(zeros)) wanted.push([t.file, zero, t.length_ms]);
    }
    for (const [file, zero, length] of wanted) {
      const from = Math.max(0, t0 - zero), to = Math.min(length, t1 - zero);
      const have = samples[file];
      if (to <= from || (have && from >= have.from && to <= have.to)) continue;
      const reply = await fetch(q("/samples", `&file=${encodeURIComponent(file)}&from=${from - 50}&to=${to + 50}`));
      if (!reply.ok) continue;
      const buf = await reply.arrayBuffer();
      const head = new Float32Array(buf, 0, 2);
      const data = new Float32Array(buf.slice(8));
      const start = Math.max(0, head[0]);
      samples[file] = { from: start, to: start + data.length / 2 / head[1] * 1000, data };
      needDraw = true;
    }
  }, 150);
}

// ---- the playhead, every frame

function position(now) {
  return play.playing ? play.base + (now - play.baseAt) : play.pos;
}

function frame(now) {
  if (needDraw) draw();
  if (state) {
    const pos = position(now);
    const w = width();
    if (play.playing && follow) {  // keep it in view: turn the page before it leaves
      const x = toX(pos);
      if (x > w - 40 || x < LABEL) { view.start = pos - 0.1 * (w - LABEL) * view.msPerPx; draw(); }
    }
    const ctx = over.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, over.width / dpr, over.height / dpr);
    const x = toX(pos);
    if (x >= LABEL && x <= w) {
      ctx.fillStyle = state.colours.playhead;
      ctx.fillRect(Math.round(x) - 1, 0, 2, over.height / dpr);
    }
    document.getElementById("where").textContent = fmt(Math.max(0, pos));
  }
  requestAnimationFrame(frame);
}

// ---- what gout reports

function listen() {
  const events = new EventSource(q("/events"));
  events.onmessage = (e) => {
    document.getElementById("gone").hidden = true;
    const m = JSON.parse(e.data);
    const now = performance.now();
    if (m.v !== version) loadState();
    if (m.playing) {
      const predicted = play.playing ? play.base + (now - play.baseAt) : m.pos;
      const off = m.pos - predicted;
      if (!play.playing || Math.abs(off) > 60) { play.base = m.pos; play.baseAt = now; }  // a jump: follow it
      else { play.base += off * 0.15; }  // drift: ease towards what gout says, no stutter
    }
    play.playing = m.playing;
    play.pos = m.pos;
    const mode = document.getElementById("mode");
    if (m.take) {
      if (!take || take.at !== m.take.at) take = { at: m.take.at, label: m.take.label, peaks: [] };
      take.peaks.length = m.take.from;
      take.peaks.push(...m.take.peaks);
      mode.textContent = m.take.label === "check" ? "● CHECK" : "● REC";
      mode.className = "rec";
      if (m.take.peaks.length) needDraw = true;
    } else {
      if (take) { take = null; needDraw = true; }
      mode.textContent = m.playing ? "▶ playing" : "■";
      mode.className = "";
    }
  };
  events.onerror = () => { document.getElementById("gone").hidden = false; };
}

// ---- keys and the mouse

addEventListener("keydown", (e) => {
  if (e.ctrlKey && (e.key === "r" || e.key === "R")) { e.preventDefault(); send({ key: "record" }); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const w = width();
  const middle = Math.max(LABEL, Math.min(w, toX(position(performance.now()))));
  switch (e.key) {
    case " ": e.preventDefault(); follow = true; send({ key: "space" }); break;
    case "ArrowLeft": e.preventDefault(); send({ key: "left" }); break;
    case "ArrowRight": e.preventDefault(); send({ key: "right" }); break;
    case "+": case "=": zoomAt(middle, 0.5); break;
    case "-": case "_": zoomAt(middle, 2); break;
    case "0": fit(); break;
    case "f": follow = !follow; break;
    case "Home": view.start = 0; needDraw = true; break;
    default: return;
  }
});

over.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = over.getBoundingClientRect();
  const x = e.clientX - rect.left;
  if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    view.start += (e.shiftKey ? e.deltaY : e.deltaX) * view.msPerPx;
    follow = false;
    needDraw = true;
  } else {
    zoomAt(Math.max(LABEL, x), Math.exp(e.deltaY * 0.0015));
  }
}, { passive: false });

let drag = null;
over.addEventListener("mousedown", (e) => {
  drag = { x: e.clientX, start: view.start, moved: false };
});
addEventListener("mousemove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x;
  if (Math.abs(dx) > 3) drag.moved = true;
  if (drag.moved) { view.start = drag.start - dx * view.msPerPx; follow = false; needDraw = true; }
});
addEventListener("mouseup", (e) => {
  if (drag && !drag.moved) {
    const rect = over.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (x >= LABEL) send({ seek: Math.max(0, Math.round(fromX(x))) });
  }
  drag = null;
});

addEventListener("resize", () => { needDraw = true; });

loadState().then(() => { listen(); requestAnimationFrame(frame); });
