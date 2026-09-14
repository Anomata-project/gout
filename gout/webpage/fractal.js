/* The fractal screen of gout's web preview: examples/addons/fractal.py, in the browser.

   Every character is a starting point z on the complex plane; Newton's method walks it to a
   root of the formula. The character says how many steps it took, its colour which root it
   reached. The bass bends the method and pushes the rotation, the level zooms, hits and highs
   make it denser, the mids trade the colours. The server reads and differentiates the formula
   (gout/formula.py) and sends it as numbered steps; this file runs them. No code is made from
   text here either. Complex division, powers and Horner's rule follow CPython, so a frame comes
   out as the addon draws it in the terminal. */
"use strict";

(function (root) {
  const RAMP = " .:-=+*#%@";
  const LIMIT = 24;
  const KEYS = "1234567890";
  const FPS = 20;
  const STATUS_SECONDS = 3;
  const CHOICE = "gout.fractal";

  const PRESETS = [  // as in fractal.py
    { name: "seven", formula: "z^3 + 7", about: "three basins" },
    { name: "classic", formula: "z^3 - 1", about: "the classic Newton fractal" },
    { name: "four", formula: "z^4 - 1", about: "four-fold" },
    { name: "star", formula: "z^5 - 1", about: "a five-pointed star" },
    { name: "rings", formula: "z^8 + 15z^4 - 16", about: "two rings of four roots" },
    { name: "islands", formula: "z^3 - 2z + 2", about: "small dark islands where Newton never settles", zoom: 1.3, center: [0.4, 0] },
    { name: "twins", formula: "z^6 + z^3 - 1", about: "two rings of three roots" },
    { name: "lopsided", formula: "z^5 - z - 1", about: "mirrored only top to bottom" },
    { name: "waves", formula: "sin(z)", about: "an endless row of roots", zoom: 5 },
    { name: "ladder", formula: "cosh(z) - 2", about: "two mirrored columns of roots", zoom: 6 },
  ];

  // ---------------------------------------------------------------- complex numbers, as CPython

  function quot(ar, ai, br, bi, out) {
    const absr = Math.abs(br), absi = Math.abs(bi);
    if (absr >= absi) {
      if (absr === 0) { out[0] = NaN; out[1] = NaN; return; }  // ZeroDivisionError
      const ratio = bi / br, denom = br + bi * ratio;
      out[0] = (ar + ai * ratio) / denom;
      out[1] = (ai - ar * ratio) / denom;
    } else if (absi >= absr) {
      const ratio = br / bi, denom = br * ratio + bi;
      out[0] = (ar * ratio + ai) / denom;
      out[1] = (ai * ratio - ar) / denom;
    } else {
      out[0] = NaN; out[1] = NaN;
    }
  }

  function powu(xr, xi, n, out) {
    let rr = 1, ri = 0, pr = xr, pi = xi, mask = 1;
    while (mask > 0 && n >= mask) {
      if (n & mask) { const t = rr * pr - ri * pi; ri = rr * pi + ri * pr; rr = t; }
      mask <<= 1;
      const t = pr * pr - pi * pi; pi = pr * pi + pi * pr; pr = t;
    }
    out[0] = rr; out[1] = ri;
  }

  function powi(xr, xi, n, out) {
    if (n > 0) return powu(xr, xi, n, out);
    powu(xr, xi, -n, out);
    quot(1, 0, out[0], out[1], out);
  }

  function pow(ar, ai, br, bi, out) {
    if (bi === 0 && br === Math.floor(br) && Math.abs(br) <= 100) return powi(ar, ai, br, out);
    if (br === 0 && bi === 0) { out[0] = 1; out[1] = 0; return; }
    if (ar === 0 && ai === 0) {
      if (bi !== 0 || br < 0) { out[0] = NaN; out[1] = NaN; return; }
      out[0] = 0; out[1] = 0; return;
    }
    const vabs = Math.hypot(ar, ai);
    let len = Math.pow(vabs, br);
    const at = Math.atan2(ai, ar);
    let phase = at * br;
    if (bi !== 0) {
      len /= Math.exp(at * bi);
      phase += bi * Math.log(vabs);
    }
    out[0] = len * Math.cos(phase); out[1] = len * Math.sin(phase);
  }

  const CALLS = {
    sin(r, i, o) { o[0] = Math.sin(r) * Math.cosh(i); o[1] = Math.cos(r) * Math.sinh(i); },
    cos(r, i, o) { o[0] = Math.cos(r) * Math.cosh(i); o[1] = -Math.sin(r) * Math.sinh(i); },
    tan(r, i, o) {  // (t + iu) / (1 - itu) with t = tan r, u = tanh i
      const t = Math.tan(r), u = Math.tanh(i), c = 1 / Math.cosh(i), tu = t * u;
      const denom = 1 + tu * tu;
      o[0] = ((t / denom) * c) * c; o[1] = u * (1 + t * t) / denom;
    },
    sinh(r, i, o) { o[0] = Math.cos(i) * Math.sinh(r); o[1] = Math.sin(i) * Math.cosh(r); },
    cosh(r, i, o) { o[0] = Math.cos(i) * Math.cosh(r); o[1] = Math.sin(i) * Math.sinh(r); },
    tanh(r, i, o) {  // (t + iu) / (1 + itu) with t = tanh r, u = tan i
      const tx = Math.tanh(r), ty = Math.tan(i), cx = 1 / Math.cosh(r), txty = tx * ty;
      const denom = 1 + txty * txty;
      o[0] = tx * (1 + ty * ty) / denom; o[1] = ((ty / denom) * cx) * cx;
    },
    exp(r, i, o) { const l = Math.exp(r); o[0] = l * Math.cos(i); o[1] = l * Math.sin(i); },
    log(r, i, o) { o[0] = Math.log(Math.hypot(r, i)); o[1] = Math.atan2(i, r); },
    sqrt(r, i, o) {
      const ax = Math.abs(r), ay = Math.abs(i);
      if (ax === 0 && ay === 0) { o[0] = 0; o[1] = i; return; }
      const s = 2 * Math.sqrt(ax / 8 + Math.hypot(ax / 8, ay / 8));
      const d = ay / (2 * s);
      if (r >= 0) { o[0] = s; o[1] = i < 0 || Object.is(i, -0) ? -d : d; }
      else { o[0] = d; o[1] = i < 0 || Object.is(i, -0) ? -s : s; }
    },
  };

  // ---------------------------------------------------------------- Newton's method

  /* newton(program)(re, im, limit, relax, eps) -> {steps, finalsRe, finalsIm}: every point at once,
     re and im changed in place; steps is limit where a point never settled. */
  function newton(program) {
    const tmp = [0, 0];
    let slope;
    if (program.poly) {
      const c = program.poly;
      const deg = c.length - 1;
      slope = function (zr, zi, out) {  // Horner's rule, as formula.horner writes it
        let wr = c[deg][0], wi = c[deg][1], dr = 0, di = 0;
        for (let k = deg - 1; k >= 0; k--) {
          let t = dr * zr - di * zi; di = dr * zi + di * zr; dr = t + wr; di = di + wi;
          t = wr * zr - wi * zi; wi = wr * zi + wi * zr; wr = t + c[k][0]; wi = wi + c[k][1];
        }
        quot(wr, wi, dr, di, out);
      };
    } else {
      const steps = program.steps;
      for (const step of steps) {
        const op = step[0];
        if (!["z", "num", "neg", "add", "sub", "mul", "div", "pow", "powi", "call"].includes(op)) throw new Error(`fractal: unknown step ${op}`);
        if (op === "call" && !CALLS[step[1]]) throw new Error(`fractal: unknown function ${step[1]}`);
      }
      const n = steps.length;
      const vr = new Float64Array(n), vi = new Float64Array(n);
      const value = program.value, slopeAt = program.slope;
      slope = function (zr, zi, out) {
        for (let k = 0; k < n; k++) {
          const s = steps[k];
          switch (s[0]) {
            case "z": vr[k] = zr; vi[k] = zi; break;
            case "num": vr[k] = s[1]; vi[k] = s[2]; break;
            case "neg": vr[k] = -vr[s[1]]; vi[k] = -vi[s[1]]; break;
            case "add": vr[k] = vr[s[1]] + vr[s[2]]; vi[k] = vi[s[1]] + vi[s[2]]; break;
            case "sub": vr[k] = vr[s[1]] - vr[s[2]]; vi[k] = vi[s[1]] - vi[s[2]]; break;
            case "mul": {
              const ar = vr[s[1]], ai = vi[s[1]], br = vr[s[2]], bi = vi[s[2]];
              vr[k] = ar * br - ai * bi; vi[k] = ar * bi + ai * br; break;
            }
            case "div": quot(vr[s[1]], vi[s[1]], vr[s[2]], vi[s[2]], tmp); vr[k] = tmp[0]; vi[k] = tmp[1]; break;
            case "pow": pow(vr[s[1]], vi[s[1]], vr[s[2]], vi[s[2]], tmp); vr[k] = tmp[0]; vi[k] = tmp[1]; break;
            case "powi": powi(vr[s[1]], vi[s[1]], s[2], tmp); vr[k] = tmp[0]; vi[k] = tmp[1]; break;
            case "call": CALLS[s[1]](vr[s[2]], vi[s[2]], tmp); vr[k] = tmp[0]; vi[k] = tmp[1]; break;
          }
        }
        quot(vr[value], vi[value], vr[slopeAt], vi[slopeAt], out);
      };
    }
    return function (re, im, limit, relax, eps) {
      const count = re.length;
      const steps = new Int32Array(count).fill(limit);
      const fr = new Float64Array(count).fill(NaN), fi = new Float64Array(count).fill(NaN);
      let active = new Int32Array(count);
      for (let i = 0; i < count; i++) active[i] = i;
      let size = count;
      const s = [0, 0];
      for (let n = 0; n < limit && size; n++) {
        let kept = 0;
        for (let a = 0; a < size; a++) {
          const i = active[a];
          const zr = re[i], zi = im[i];
          slope(zr, zi, s);
          if (!Number.isFinite(s[0]) || !Number.isFinite(s[1])) continue;  // a pole, or an overflow
          if (Math.hypot(s[0], s[1]) < eps) {
            steps[i] = n; fr[i] = zr; fi[i] = zi;
            continue;
          }
          re[i] = zr - relax * s[0];
          im[i] = zi - relax * s[1];
          active[kept++] = i;
        }
        size = kept;
      }
      return { steps, fr, fi };
    };
  }

  // ---------------------------------------------------------------- the song, as analysis.Features

  function features(payload) {
    const out = { frameMs: payload.frame_ms, sums: {} };
    for (const [name, text] of Object.entries(payload.bands)) {
      const raw = atob(text);
      const sums = new Float64Array(raw.length + 1);
      let total = 0;
      for (let i = 0; i < raw.length; i++) {
        total += raw.charCodeAt(i) / 255;
        sums[i + 1] = total;
      }
      out.sums[name] = sums;
    }
    return out;
  }

  function band(f, name, ms, windowMs) {
    if (!f) return 0;
    const F = f.frameMs, sums = f.sums[name], last = sums.length - 1;
    windowMs = Math.max(F, windowMs);
    const hi = Math.min(last, Math.max(0, Math.floor(ms / F) + 1));
    const lo = Math.min(last, Math.max(0, Math.floor((ms - windowMs) / F) + 1));
    return (sums[hi] - sums[lo]) / Math.max(1, Math.round(windowMs / F));
  }

  function travel(f, name, ms) {
    if (!f) return 0;
    const F = f.frameMs, sums = f.sums[name];
    return sums[Math.min(sums.length - 1, Math.max(0, Math.floor(ms / F)))] * F / 1000;
  }

  // ---------------------------------------------------------------- the picture

  class Fractal {
    constructor(fetchProgram) {
      this.fetchProgram = fetchProgram;
      this.programs = new Map();   // formula text -> {pretty, newton}
      this.current = 0;
      this.custom = null;          // {name, formula, pretty}
      this.colours = true;
      this.zoomFactor = 1;
      this.step = 1;
      this.reset();
    }

    reset() {
      this.framing = null;
      this.roots = [];
      this.rootCells = new Map();
      this.last = null;
      this.zoomFactor = 1;
    }

    preset() {
      return this.custom || PRESETS[this.current];
    }

    choose(index) {
      this.current = ((index % PRESETS.length) + PRESETS.length) % PRESETS.length;
      this.custom = null;
      this.reset();
    }

    find(word) {
      word = String(word).trim().toLowerCase();
      if (word.length === 1 && KEYS.slice(0, PRESETS.length).includes(word)) return KEYS.indexOf(word);
      const at = PRESETS.findIndex((p) => p.name === word);
      return at < 0 ? null : at;
    }

    async program(text) {
      if (!this.programs.has(text)) {
        const reply = await this.fetchProgram(text);
        this.programs.set(text, { pretty: reply.pretty, newton: newton(reply.program) });
      }
      return this.programs.get(text);
    }

    rootOf(zr, zi, tolerance) {
      const cell = `${Math.round(zr / tolerance)},${Math.round(zi / tolerance)}`;
      let index = this.rootCells.get(cell);
      if (index === undefined) {
        index = this.roots.findIndex(([r, i]) => Math.hypot(r - zr, i - zi) < 5 * tolerance);
        if (index < 0) {
          this.roots.push([zr, zi]);
          index = this.roots.length - 1;
        }
        this.rootCells.set(cell, index);
      }
      return index;
    }

    frameFor(p, run) {
      if (!this.framing) {
        let [cr, ci] = p.center || [0, 0];
        if (p.zoom) {
          this.framing = [cr, ci, p.zoom];
        } else {
          const w = 48, h = 24, re = new Float64Array(w * h), im = new Float64Array(w * h);
          const turn = 4.0 / (h / 2);
          for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
              re[y * w + x] = cr + (x - w / 2) * 0.5 * turn;
              im[y * w + x] = ci + (h / 2 - y) * turn;
            }
          }
          const { fr, fi } = run(re, im, 40, 1.0, 1e-7);
          const found = [];
          for (let k = 0; k < fr.length; k++) {
            if (Number.isNaN(fr[k])) continue;
            if (Math.hypot(fr[k] - cr, fi[k] - ci) < 6 && found.every(([r, i]) => Math.hypot(fr[k] - r, fi[k] - i) > 1e-3)) {
              found.push([fr[k], fi[k]]);
            }
          }
          if (!p.center && found.length) {
            cr = found.reduce((a, r) => a + r[0], 0) / found.length;
            ci = found.reduce((a, r) => a + r[1], 0) / found.length;
          }
          const spread = found.length ? Math.max(...found.map(([r, i]) => Math.hypot(r - cr, i - ci))) : 1.0;
          this.framing = [cr, ci, Math.max(0.6, spread * 1.7)];
        }
      }
      return this.framing;
    }

    /* height rows of [text, classes], as Fractal.frame in the addon; null while the formula loads */
    frame(song, positionMs, width, height, run) {
      const t = positionMs / 1000;
      const low = band(song, "low", positionMs, 120), high = band(song, "high", positionMs, 80);
      const level = band(song, "level", positionMs, 400), onset = band(song, "onset", positionMs, 60);
      const [cr, ci, baseHalf] = this.frameFor(this.preset(), run);
      const theta = 0.06 * t + 0.5 * travel(song, "low", positionMs) + 0.15 * travel(song, "high", positionMs);
      const half = baseHalf * this.zoomFactor * (1.25 - 0.45 * level);
      const relax = 1.0 + 0.3 * low;
      const lift = 1.5 * high + 2.0 * onset;
      const hue = Math.floor(travel(song, "mid", positionMs) / 3);
      const inputs = [width, height, this.preset().formula, this.colours, theta.toFixed(5), half.toFixed(6),
                      relax.toFixed(4), lift.toFixed(3), hue].join("|");
      if (this.last && this.last[0] === inputs) return this.last[1];
      const began = performance.now();
      const step = this.step;
      const cols = Math.ceil(width / step);
      const k = half / (height / 2), tr = Math.cos(theta) * k, ti = Math.sin(theta) * k;
      const re = new Float64Array(cols * height), im = new Float64Array(cols * height);
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < cols; x++) {
          const pr = (x * step - width / 2) * 0.5, pi = height / 2 - y;
          re[y * cols + x] = cr + (pr * tr - pi * ti);
          im[y * cols + x] = ci + (pr * ti + pi * tr);
        }
      }
      const { steps, fr, fi } = run(re, im, LIMIT, relax, half * 2e-4);
      const spent = (performance.now() - began) / 1000;
      if (spent > 1.4 / FPS && this.step < 3) this.step += 1;
      else if (spent < 0.4 / FPS && this.step > 1) this.step -= 1;
      const settled = [];
      for (let i = 0; i < steps.length; i += 7) if (steps[i] < LIMIT) settled.push(steps[i]);
      settled.sort((a, b) => a - b);
      if (!settled.length) settled.push(0);
      const fast = settled[0], slow = settled[Math.floor(settled.length * 0.97)];
      const scale = (RAMP.length - 1) / Math.max(1, slow - fast);
      const tolerance = this.framing[2] * 1e-3;  // fixed per formula, so a root keeps its colour
      const rows = [];
      for (let y = 0; y < height; y++) {
        let text = "", classes = "";
        const base = y * cols;
        for (let x = 0; x < width; x++) {
          const i = base + Math.floor(x / step);
          const s = steps[i];
          if (s >= LIMIT) { text += " "; classes += " "; continue; }
          text += RAMP[Math.min(RAMP.length - 1, Math.max(1, Math.trunc((s - fast) * scale + lift)))];
          classes += this.colours ? String((this.rootOf(fr[i], fi[i], tolerance) + hue) % 10) : " ";
        }
        rows.push([text, classes]);
      }
      this.last = [inputs, rows];
      return rows;
    }
  }

  // ---------------------------------------------------------------- the screen in the page

  const screen = {
    fractal: null,
    host: null,
    open: false,
    song: null,
    songState: null,
    note: "",
    statusUntil: 0,
    fullscreen: false,
    raf: 0,
    lastFrame: 0,
    font: { size: 16, width: 9.6, height: 19 },
  };

  function el(id) {
    return document.getElementById(id);
  }

  async function command(host, argv) {
    const f = screen.fractal;
    const text = argv.join(" ").trim();
    if (!text) return true;
    if (text === "presets" || text === "list") {
      const lines = PRESETS.map((p, i) => ` ${i === f.current && !f.custom ? "▸" : " "}${KEYS[i] || " "} ${p.name.padEnd(10)} ${p.formula.padEnd(22)} ${p.about}`);
      lines.push("   fractal N, fractal NAME, or fractal FORMULA");
      host.say(lines);
      return false;
    }
    const found = f.find(text);
    if (found !== null) {
      f.choose(found);
      remember();
      return true;
    }
    const reply = await host.api("GET", `formula?text=${encodeURIComponent(text)}`);
    if (!reply.program) {
      host.say(reply.lines && reply.lines.length ? reply.lines : [`fractal: cannot read ${text}`]);
      return false;
    }
    f.programs.set(reply.text, { pretty: reply.pretty, newton: newton(reply.program) });
    f.custom = { name: "custom", formula: reply.text, pretty: reply.pretty };
    f.reset();
    host.say(`fractal: w = ${reply.pretty}`);
    return true;
  }

  function remember() {
    try { localStorage.setItem(CHOICE, PRESETS[screen.fractal.current].name); } catch (e) { /* private mode */ }
  }

  function palette() {
    const style = getComputedStyle(document.documentElement);
    const colours = [];
    for (let i = 0; i < 10; i++) {
      const c = style.getPropertyValue(`--track-${i}`).trim();
      if (!c) break;
      colours.push(c);
    }
    return colours.length ? colours : ["#5fafd7", "#87d787", "#d787af", "#afafff", "#ffaf5f", "#5fd7af"];
  }

  function layout() {
    const canvas = el("fractal");
    const ratio = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const size = Math.max(10, Math.min(18, Math.round(w / 150 / 0.6)));
    ctx.font = `bold ${size}px ${getComputedStyle(document.body).fontFamily}`;
    const width = ctx.measureText("0".repeat(10)).width / 10 || size * 0.6;
    screen.font = { size, width, height: Math.round(size * 1.2) };
  }

  async function ensureSong(host) {
    const state = host.mixState();
    if (!state || state === screen.songState) return;
    screen.songState = state;
    screen.note = "listening to the song…";
    const reply = await host.api("GET", "analysis");
    if (reply.bands && screen.songState === state) {
      screen.song = features(reply);
      screen.note = "";
    } else if (!reply.bands) {
      screen.note = "";
    }
  }

  function draw(now) {
    if (!screen.open) return;
    screen.raf = requestAnimationFrame(draw);
    const host = screen.host, f = screen.fractal;
    const playing = host.playing();
    if (playing && now - screen.lastFrame < 1000 / FPS) return;
    screen.lastFrame = now;
    const canvas = el("fractal");
    const ctx = canvas.getContext("2d");
    const { width: cw, height: ch } = screen.font;
    const cols = Math.max(10, Math.floor(canvas.clientWidth / cw));
    const rows = Math.max(5, Math.floor(canvas.clientHeight / ch));
    const p = f.preset();
    const program = f.programs.get(p.formula);
    if (!program) {
      f.program(p.formula).catch((e) => { screen.note = String(e.message || e); });
      return;
    }
    let picture;
    try {
      picture = f.frame(screen.song, host.position(), cols, rows, program.newton);
    } catch (e) {  // as the ui does with a screen that raises
      host.say(`error: fractal screen: ${e.message || e}`);
      close();
      return;
    }
    if (picture !== screen.drawn) {
      screen.drawn = picture;
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, canvas.clientWidth, canvas.clientHeight);
      const colours = palette();
      const plain = "#d0d0d0";
      ctx.textBaseline = "top";
      const left = Math.floor((canvas.clientWidth - cols * cw) / 2), top = Math.floor((canvas.clientHeight - rows * ch) / 2);
      for (let y = 0; y < picture.length; y++) {
        const [text, classes] = picture[y];
        let x = 0;
        while (x < text.length) {
          const k = classes[x];
          let end = x;
          while (end < text.length && classes[end] === k) end++;
          const run = text.slice(x, end);
          if (run.trim()) {
            ctx.fillStyle = k === " " ? plain : colours[Number(k) % colours.length];
            ctx.fillText(run, left + x * cw, top + y * ch);
          }
          x = end;
        }
      }
    }
    const status = el("screen-status");
    const show = !playing || performance.now() < screen.statusUntil;
    status.hidden = !show;
    if (show) {
      const label = f.custom ? "custom" : `${KEYS[f.current] || ""} ${p.name}`.trim();
      const pretty = f.custom ? f.custom.pretty : program.pretty;
      const where = fmt(host.position()), length = fmt(host.length());
      const note = screen.note ? `   ${screen.note}` : "";
      status.textContent = ` ${label}  w = ${pretty}   ${playing ? "▶" : "■"} ${where} / ${length}${note}   esc back`;
    }
  }

  function fmt(ms) {
    ms = Math.max(0, Math.round(ms || 0));
    const pad = (n, w) => String(n).padStart(w, "0");
    return `${pad(Math.floor(ms / 3600000), 2)}:${pad(Math.floor(ms / 60000) % 60, 2)}:${pad(Math.floor(ms / 1000) % 60, 2)}.${pad(ms % 1000, 3)}`;
  }

  function onKey(e) {
    if (!screen.open) return;
    const host = screen.host, f = screen.fractal;
    screen.statusUntil = performance.now() + STATUS_SECONDS * 1000;
    const key = e.key;
    if (key === "Escape" || (e.ctrlKey && (key === " " || e.code === "Space"))) {
      e.preventDefault();
      return close();
    }
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    let used = true;
    if (key === " ") host.togglePlay();
    else if (key === "ArrowLeft" || key === "ArrowRight") host.seek(key === "ArrowLeft" ? -5000 : 5000);
    else if (KEYS.includes(key) && KEYS.indexOf(key) < PRESETS.length) { f.choose(KEYS.indexOf(key)); remember(); }
    else if (key === "ArrowUp" || key === "ArrowDown") { f.choose(f.current + (key === "ArrowDown" ? 1 : -1)); remember(); }
    else if (key === "+" || key === "=") { f.zoomFactor /= 1.25; f.last = null; }
    else if (key === "-" || key === "_") { f.zoomFactor *= 1.25; f.last = null; }
    else if (key === "c") { f.colours = !f.colours; f.last = null; }
    else used = false;
    if (used) {
      e.preventDefault();
      screen.drawn = null;
    }
  }

  async function open(host) {
    if (screen.open) return;
    if (!screen.fractal) {
      screen.fractal = new Fractal(async (text) => {
        const reply = await host.api("GET", `formula?text=${encodeURIComponent(text)}`);
        if (!reply.program) throw new Error((reply.lines || [])[0] || `fractal: cannot read ${text}`);
        return reply;
      });
      let chosen = null;
      try { chosen = localStorage.getItem(CHOICE); } catch (e) { /* private mode */ }
      const at = chosen === null ? null : screen.fractal.find(chosen);
      if (at !== null) screen.fractal.choose(at);
    }
    screen.host = host;
    try {
      if (!(await command(host, host.argv))) return;
    } catch (e) {
      return host.say(`error: fractal: ${e.message || e}`);
    }
    if (!host.hasTracks()) return host.say("fractal: no tracks yet, nothing to play — upload a file first");
    screen.open = true;
    screen.drawn = null;
    const box = el("screen");
    box.hidden = false;
    screen.fullscreen = false;
    if (box.requestFullscreen && !document.fullscreenElement) {
      box.requestFullscreen().then(() => { screen.fullscreen = true; layout(); screen.drawn = null; }).catch(() => {});
    }
    layout();
    screen.statusUntil = performance.now() + STATUS_SECONDS * 1000;
    document.addEventListener("keydown", onKey, true);
    screen.raf = requestAnimationFrame(draw);
    if (!host.playing()) {
      screen.note = "rendering the mix…";
      const started = await host.startPlaying();
      screen.note = started === false ? "press space to play" : "";
    }
    await ensureSong(host);
  }

  function close() {
    if (!screen.open) return;
    screen.open = false;
    cancelAnimationFrame(screen.raf);
    document.removeEventListener("keydown", onKey, true);
    el("screen").hidden = true;
    if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
    screen.host.closed();
  }

  if (typeof document !== "undefined") {  // in a page; node loads this file for the tests
    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement && screen.open && screen.fullscreen) close();  // esc in fullscreen
    });
    window.addEventListener("resize", () => {
      if (!screen.open) return;
      layout();
      screen.drawn = null;
      if (screen.fractal) screen.fractal.last = null;
    });
  }

  const api = {
    open,
    close,
    names: () => PRESETS.map((p) => p.name),
    // for tests
    newton, features, band, travel, Fractal, PRESETS, LIMIT, RAMP, complex: { quot, pow, powi, calls: CALLS },
  };
  root.GoutFractal = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
