/* gout's web preview: the prompt, the log, the timeline, the files and the player.

   The project's key lives in localStorage and goes to the server in the X-Gout-Key header.
   Commands run on the server exactly as gout runs them; the page keeps its own words (play,
   stop, view, cheat, help, clear, fractal) and its keys, as the terminal ui does. */
"use strict";

(function () {
  const KEY = "gout.key";
  const HISTORY = "gout.history";
  const PREFS = "gout.prefs";
  const HISTORY_KEEP = 500;
  const LOG_KEEP = 2000;

  const $ = (id) => document.getElementById(id);
  const saved = {
    get(name) { try { return localStorage.getItem(name); } catch (e) { return null; } },
    set(name, value) { try { localStorage.setItem(name, value); } catch (e) { /* private mode */ } },
    remove(name) { try { localStorage.removeItem(name); } catch (e) { /* private mode */ } },
  };

  const ui = {
    key: null,
    info: { limits: { files: 5, file_bytes: 20000000, days: 7 }, commands: [], aliases: {}, effects: {}, downloads: {} },
    project: null,          // the last state from the server
    panel: null,            // {track, kind}: what the effect panel shows
    pictures: true,
    showTimeline: true,
    showCheat: true,
    log: [],
    history: [],
    historyAt: -1,
    draft: "",
    busy: 0,
    rendering: false,
    creating: null,
    playhead: 0,            // project ms
    playing: false,
    mix: { url: null, blob: null, state: null, headMs: 0 },
    cheat: null,
    charWidth: 8.4,
  };

  // ------------------------------------------------------------------ talking to the server

  async function api(method, name, body, options) {
    options = options || {};
    const headers = {};
    if (ui.key) headers["X-Gout-Key"] = ui.key;
    let data = options.data;
    if (body !== undefined) {
      data = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
    }
    let reply;
    try {
      reply = await fetch("api/" + name, { method, headers, body: data, cache: "no-store" });
    } catch (e) {
      return { ok: false, status: 0, lines: ["error: the server cannot be reached"] };
    }
    if (options.raw && reply.ok) return reply;
    let json;
    try {
      json = await reply.json();
    } catch (e) {
      json = { ok: false, lines: [`error: the server answered ${reply.status}`] };
    }
    json.status = reply.status;
    if (reply.status === 401 && json.gone) {
      await projectGone();
      json.lines = [];
    }
    return json;
  }

  function sent() {
    return { width: timelineWidth(), panel: ui.panel, pictures: ui.pictures };
  }

  async function createProject() {
    if (ui.creating) return ui.creating;
    ui.creating = (async () => {
      const reply = await api("POST", "project", {});
      if (reply.status !== 201) {
        say(reply.lines && reply.lines.length ? reply.lines : ["error: could not make a project"]);
        return false;
      }
      ui.key = reply.key;
      saved.set(KEY, reply.key);
      say([`new project in this browser: upload up to ${ui.info.limits.files} mp3 or wav files with + upload,`,
           "then try  ls  gain 1 -6  eq 1 hp80  and space to play. help shows the rest."]);
      await refresh();
      return true;
    })();
    try {
      return await ui.creating;
    } finally {
      ui.creating = null;
    }
  }

  async function projectGone() {
    if (!ui.key) return;
    ui.key = null;
    saved.remove(KEY);
    forgetMix();
    say(["this browser's project is gone (unused for a week, or deleted); starting a new one"]);
    await createProject();
  }

  async function refresh() {
    if (!ui.key) return;
    const reply = await api("POST", "state", sent());
    if (reply.state) apply(reply.state);
  }

  function apply(state) {
    ui.project = state;
    ui.panel = state.panel ? { track: state.panel.track, kind: state.panel.kind } : null;
    draw();
  }

  // ------------------------------------------------------------------ the log and the prompt

  function say(lines) {
    for (const line of [].concat(lines || [])) {
      for (const part of String(line).split("\n")) ui.log.push(part);
    }
    if (ui.log.length > LOG_KEEP) ui.log.splice(0, ui.log.length - LOG_KEEP);
    drawLog();
  }

  function drawLog() {
    const box = $("log");
    const frag = document.createDocumentFragment();
    for (const line of ui.log) {
      const div = document.createElement("div");
      div.textContent = line;
      if (line.startsWith("> ")) div.className = "r-command_echo";
      else if (line.startsWith("error")) div.className = "r-error";
      frag.appendChild(div);
    }
    box.replaceChildren(frag);
    const scroll = $("scroll");
    scroll.scrollTop = scroll.scrollHeight;
  }

  function setBusy(delta) {
    ui.busy = Math.max(0, ui.busy + delta);
    $("prompt").textContent = ui.busy ? "… " : "> ";
    $("prompt").className = ui.busy ? "r-suggestion" : "r-prompt";
  }

  function words(line) {
    const out = [];
    const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
    let m;
    while ((m = re.exec(line))) out.push(m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3]);
    return out;
  }

  function parseTime(text) {
    const t = String(text).trim();
    let m;
    if ((m = t.match(/^(\d+(?:\.\d+)?)ms$/))) return Number(m[1]);
    if ((m = t.match(/^(\d+(?:\.\d+)?)s$/))) return Number(m[1]) * 1000;
    if ((m = t.match(/^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$/))) return ((Number(m[1] || 0) * 60 + Number(m[2])) * 60 + Number(m[3])) * 1000;
    if ((m = t.match(/^\d+(?:\.\d+)?$/))) return Number(t) * 60000;  // a bare number is minutes, as in gout
    return null;
  }

  function fmtMs(ms) {
    ms = Math.max(0, Math.round(ms));
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, s = Math.floor(ms / 1000) % 60;
    const pad = (n, w) => String(n).padStart(w, "0");
    return `${pad(h, 2)}:${pad(m, 2)}:${pad(s, 2)}.${pad(ms % 1000, 3)}`;
  }

  async function submit() {
    const input = $("line");
    const line = input.value.trim();
    input.value = "";
    ui.historyAt = -1;
    if (!line) return;
    if (ui.history[ui.history.length - 1] !== line) ui.history.push(line);
    if (ui.history.length > HISTORY_KEEP) ui.history.splice(0, ui.history.length - HISTORY_KEEP);
    saved.set(HISTORY, JSON.stringify(ui.history));
    say("> " + line);
    const argv = words(line);
    if (!argv.length) return;
    const head = ui.info.aliases[argv[0]] || argv[0];
    switch (head) {
      case "play":
        if (argv.length > 1) {
          const at = parseTime(argv[1]);
          if (at === null) return say(`error: cannot read the time ${argv[1]}`);
          return startPlaying(at);
        }
        return startPlaying();
      case "stop":
        if (ui.playing) return stopPlaying();
        ui.playhead = 0;
        drawTimeline();
        return say("stop  playhead back to the start");
      case "view": case "timeline":
        return toggle("timeline");
      case "cheat":
        return toggle("cheat");
      case "help": case "-h": case "--help":
        if (argv[1] === "all") say("help all: the whole instruction page comes with gout (the downloads at the bottom)");
        return say(ui.cheat ? ui.cheat.lines : "help: the cheat sheet is still loading");
      case "clear":
        ui.log = [];
        return drawLog();
      case "quit": case "exit": case "q":
        return say(`quit: close the tab; the project stays in this browser (for ${ui.info.limits.days} days unused)`);
      case "sheet":
        return say("sheet: the parameter sheet is in gout itself (the downloads at the bottom)");
      case "split":
        return say("split: drag nothing, change nothing: the preview's panes are fixed");
      case "fractal": case "fz":
        return openScreen(argv.slice(1));
    }
    if (argv.length === 1 && ui.info.effects[head]) return lookAtEffect(ui.info.effects[head]);
    await runLine(line);
  }

  async function runLine(line, echo) {
    if (!ui.key && !(await createProject())) return;
    setBusy(1);
    try {
      const reply = await api("POST", "run", Object.assign({ line }, sent()));
      if (echo !== false || !reply.ok) say(reply.lines);
      if (reply.state) apply(reply.state);
    } finally {
      setBusy(-1);
    }
  }

  function lookAtEffect(kind) {
    // an effect's name alone: switch the panel to it, or its pictures on and off, as in the ui
    if (ui.panel && ui.panel.kind !== kind) {
      ui.panel.kind = kind;
    } else if (!ui.panel) {
      const first = ui.project && ui.project.tracks[0];
      if (!first) return say(`${kind}: no tracks yet — upload a file first`);
      ui.panel = { track: first.n, kind };
      ui.pictures = true;
    } else {
      ui.pictures = !ui.pictures;
    }
    savePrefs();
    refresh();
  }

  function togglePanel() {
    if (!ui.panel) {
      const first = ui.project && ui.project.tracks[0];
      if (!first) return say("ctrl-g: no tracks yet — upload a file first");
      ui.panel = { track: first.n, kind: "eq" };
      ui.pictures = true;
    } else {
      ui.pictures = !ui.pictures;
    }
    savePrefs();
    refresh();
  }

  function toggle(what) {
    if (what === "timeline") ui.showTimeline = !ui.showTimeline;
    else ui.showCheat = !ui.showCheat;
    savePrefs();
    draw();
  }

  function savePrefs() {
    saved.set(PREFS, JSON.stringify({ timeline: ui.showTimeline, cheat: ui.showCheat, pictures: ui.pictures }));
  }

  function complete() {
    const input = $("line");
    const before = input.value.slice(0, input.selectionStart);
    const after = input.value.slice(input.selectionStart);
    const parts = before.split(/\s+/);
    const word = parts[parts.length - 1];
    const first = parts.length === 1;
    let pool;
    if (first) {
      pool = ui.info.commands;
    } else {
      const head = ui.info.aliases[parts[0]] || parts[0];
      if (head === "fractal" || head === "fz") pool = ["presets"].concat(window.GoutFractal ? window.GoutFractal.names() : []);
      else if (parts.length === 2) pool = ["master", "all"].concat(ui.project ? ui.project.words : []);
      else pool = ["on", "off", "clear", "presets", "add", "move", "rm"].concat(Object.keys(ui.info.effects));
    }
    const hits = [...new Set(pool)].filter((w) => w.startsWith(word)).sort();
    if (!hits.length) return;
    let common = hits[0];
    for (const hit of hits) {
      while (!hit.startsWith(common)) common = common.slice(0, -1);
    }
    const done = hits.length === 1 ? hits[0] + " " : common;
    if (done.length > word.length) {
      input.value = before.slice(0, before.length - word.length) + done + after;
      const at = before.length - word.length + done.length;
      input.setSelectionRange(at, at);
    } else if (hits.length > 1) {
      say("  " + hits.slice(0, 40).join("  ") + (hits.length > 40 ? "  …" : ""));
    }
  }

  // ------------------------------------------------------------------ drawing

  function esc(text) {
    return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function cells(text, classes) {
    // runs of one class become one span, as the ui draws runs with one attribute
    const chars = Array.from(text);
    const kinds = Array.from(classes || "");
    let html = "";
    let i = 0;
    while (i < chars.length) {
      const k = kinds[i] || " ";
      let j = i;
      while (j < chars.length && (kinds[j] || " ") === k) j++;
      const run = esc(chars.slice(i, j).join(""));
      html += k === " " ? run : `<span class="c-${k === "." ? "dot" : k}">${run}</span>`;
      i = j;
    }
    return html;
  }

  function measure() {
    // the widest of the characters the timeline draws: a font may lack braille or box lines
    const probe = document.createElement("pre");
    probe.style.cssText = "position:absolute;visibility:hidden;left:-9999px";
    document.body.appendChild(probe);
    let widest = 0;
    for (const ch of ["0", "⣿", "┼", "┈", "─", "│", "█", "▀"]) {
      probe.textContent = ch.repeat(50);
      widest = Math.max(widest, probe.getBoundingClientRect().width / 50);
    }
    ui.charWidth = widest || 8.4;
    probe.remove();
  }

  function timelineWidth() {
    const box = $("timeline");
    return Math.max(40, Math.min(400, Math.floor((box.clientWidth || 600) / ui.charWidth) - 1));
  }

  function draw() {
    const p = ui.project;
    if (p) {
      const n = p.tracks.length;
      $("title").textContent = ` gout preview  ${p.rate} Hz  ${n} track${n === 1 ? "" : "s"}`;
      const hidden = [];
      if (!ui.showTimeline) hidden.push("alt-t timeline");
      if (!ui.showCheat) hidden.push("ctrl-k cheat sheet");
      $("title-hint").textContent = hidden.join("  ") + " ";
    }
    $("timeline-box").hidden = !ui.showTimeline;
    $("cheat-box").hidden = !ui.showCheat;
    drawTimeline();
    drawPanel();
    drawFiles();
  }

  function drawTimeline() {
    const p = ui.project;
    let state;
    if (ui.rendering) state = "  rendering the mix…";
    else if (ui.playing) state = `  ▶ ${fmtMs(ui.playhead)}  space stops`;
    else state = ui.playhead ? `  ■ ${fmtMs(ui.playhead)}  space plays` : "  space plays";
    $("timeline-title").textContent = " timeline" + state;
    if (!p || !ui.showTimeline) return;
    const t = p.timeline;
    let column = -1;
    if (t.columns && (ui.playing || ui.playhead) && ui.playhead >= t.t0 && ui.playhead <= t.t1) {
      column = Math.max(0, Math.min(t.columns - 1, Math.floor((ui.playhead - t.t0) * t.columns / (t.t1 - t.t0))));
    }
    const rows = [];
    for (const [label, text, kind, classes, role] of t.rows) {
      let chars = text, kinds = classes;
      if (column >= 0 && (kind === "ruler" || kind === "wave" || kind === "gap")) {
        const cs = Array.from(text), ks = Array.from(classes.padEnd(cs.length));
        if (cs.length === t.columns) {  // the playhead, as render_timeline draws it
          cs[column] = kind === "wave" && cs[column].trim() ? cs[column] : "│";
          ks[column] = "p";
          chars = cs.join("");
          kinds = ks.join("");
        }
      }
      const head = `<span class="r-${role || "none"}">${esc(label.padEnd(15))}</span> `;
      if (kind === "note") rows.push(head + `<span class="r-ruler_labels">${esc(text)}</span>`);
      else rows.push(head + cells(chars, kinds));
    }
    $("timeline").innerHTML = rows.join("\n");
  }

  function drawPanel() {
    const p = ui.project;
    const panel = p && p.panel;
    $("panel-box").hidden = !panel;
    if (!panel) return;
    const [head, ...rest] = panel.rows;
    $("panel-title").textContent = ` ${panel.who}  ${head[0]}`;
    const g = panel.gutter;
    $("panel").innerHTML = rest.map(([text, classes]) =>
      `<span class="r-ruler_labels">${esc(text.slice(0, g))}</span>` + cells(text.slice(g), (classes || "").slice(g))
    ).join("\n");
  }

  function drawCheat() {
    if (!ui.cheat) return;
    const headings = new Set(ui.cheat.headings);
    const second = ui.cheat.second;
    $("cheat").innerHTML = ui.cheat.lines.map((line) => {
      let html = "";
      let from = 0;
      for (const at of [0, second]) {
        if (at === null || at === undefined || at >= line.length || (at && line[at - 1] !== " ")) continue;
        const word = line.slice(at).split(" ", 1)[0];
        if (!headings.has(word)) continue;
        html += esc(line.slice(from, at)) + `<span class="r-cheat_heading">${esc(word)}</span>`;
        from = at + word.length;
      }
      return html + esc(line.slice(from));
    }).join("\n");
  }

  async function loadCheat() {
    const box = $("cheat");
    const width = Math.max(30, Math.floor((box.clientWidth || 600) / ui.charWidth) - 2);
    try {
      ui.cheat = await (await fetch(`api/cheat?width=${width}`)).json();
    } catch (e) {
      return;
    }
    drawCheat();
  }

  function size(bytes) {
    return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} kB`;
  }

  function drawFiles(pending) {
    const p = ui.project;
    const list = $("file-list");
    const limits = ui.info.limits;
    const items = [];
    for (const f of p ? p.files : []) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = f.track ? `${f.track} ${f.name}` : f.name;
      name.title = f.name;
      const bytes = document.createElement("span");
      bytes.className = "size";
      bytes.textContent = size(f.bytes);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.textContent = "✕";
      remove.title = `remove ${f.name}`;
      remove.setAttribute("aria-label", `remove ${f.name}`);
      remove.addEventListener("click", () => removeFile(f.name));
      li.append(name, bytes, remove);
      items.push(li);
    }
    for (const [name, text] of Object.entries(pending || ui.pending || {})) {
      const li = document.createElement("li");
      li.className = "pending";
      li.textContent = `${name} ${text}`;
      items.push(li);
    }
    list.replaceChildren(...items);
    const count = p ? p.files.length : 0;
    $("file-count").textContent = `${count} of ${limits.files} files · mp3 or wav, up to ${Math.round(limits.file_bytes / 1e6)} MB each`;
    $("upload-button").classList.toggle("quiet", count >= limits.files);
    $("days").textContent = limits.days;
    if (p && p.expires) {
      const day = new Date(p.expires * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long" });
      $("expires").textContent = ` (yours on ${day}, unless you use it before)`;
    }
  }

  // ------------------------------------------------------------------ files

  function uploadOne(file) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `api/files?name=${encodeURIComponent(file.name)}&width=${timelineWidth()}`);
      xhr.setRequestHeader("X-Gout-Key", ui.key);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        ui.pending = { [file.name]: `${Math.floor(100 * e.loaded / e.total)}%` };
        drawFiles();
      };
      xhr.onload = () => {
        let json;
        try {
          json = JSON.parse(xhr.responseText);
        } catch (e) {
          json = { ok: false, lines: [`error: the upload failed (${xhr.status})`] };
        }
        json.status = xhr.status;
        resolve(json);
      };
      xhr.onerror = () => resolve({ ok: false, status: 0, lines: ["error: the upload did not get through"] });
      xhr.send(file);
    });
  }

  async function uploadFiles(files) {
    if (!ui.key && !(await createProject())) return;
    const limits = ui.info.limits;
    for (const file of Array.from(files)) {
      say(`> upload ${file.name}`);
      if (!/\.(mp3|wav)$/i.test(file.name)) {
        say(`upload: ${file.name} is not mp3 or wav`);
        continue;
      }
      if (file.size > limits.file_bytes) {
        say(`upload: ${file.name} is ${(file.size / 1e6).toFixed(1)} MB; the preview takes files up to ${Math.round(limits.file_bytes / 1e6)} MB`);
        continue;
      }
      if (ui.project && ui.project.files.length >= limits.files) {
        say(`upload: the preview holds ${limits.files} files; remove one first`);
        break;
      }
      setBusy(1);
      ui.pending = { [file.name]: "0%" };
      drawFiles();
      const reply = await uploadOne(file);
      ui.pending = null;
      setBusy(-1);
      if (reply.status === 401 && reply.gone) {
        await projectGone();
        continue;
      }
      say(reply.lines);
      if (reply.state) apply(reply.state);
      else drawFiles();
    }
  }

  async function removeFile(name) {
    if (!window.confirm(`Remove ${name}?\n\nThe file and its track are deleted; undo cannot bring them back.`)) return;
    say(`> remove ${name}`);
    setBusy(1);
    try {
      const reply = await api("DELETE", `files/${encodeURIComponent(name)}`, sent());
      say(reply.lines);
      if (reply.state) apply(reply.state);
      forgetMix();
    } finally {
      setBusy(-1);
      $("line").focus();
    }
  }

  function save(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  async function downloadZip() {
    if (!ui.key) return;
    say("> download project");
    setBusy(1);
    try {
      const reply = await api("GET", "project.zip", undefined, { raw: true });
      if (!(reply instanceof Response)) return say(reply.lines);
      save(await reply.blob(), "gout-preview.zip");
      say(["download  gout-preview.zip: unzip it and run  gout -p gout-preview  (README.txt inside)"]);
    } finally {
      setBusy(-1);
    }
  }

  async function downloadMp3() {
    say("> download mix");
    if (!(await loadMix())) return;
    save(ui.mix.blob, "gout-preview.mp3");
    say(`download  gout-preview.mp3  ${size(ui.mix.blob.size)}`);
  }

  async function deleteProject() {
    if (!ui.key) return;
    if (!window.confirm("Delete this project?\n\nEvery file and setting in it is deleted from the server now. A new, empty project takes its place.")) return;
    stopPlaying();
    const reply = await api("DELETE", "project", {});
    say(reply.lines && reply.lines.length ? reply.lines : ["project deleted"]);
    ui.key = null;
    saved.remove(KEY);
    forgetMix();
    ui.playhead = 0;
    await createProject();
  }

  // ------------------------------------------------------------------ playing

  function forgetMix() {
    const audio = $("audio");
    if (ui.playing) stopPlaying();
    if (ui.mix.url) URL.revokeObjectURL(ui.mix.url);
    audio.removeAttribute("src");
    ui.mix = { url: null, blob: null, state: null, headMs: 0 };
  }

  async function loadMix() {
    const p = ui.project;
    if (ui.mix.url && p && p.mix.current && p.mix.state === ui.mix.state) return true;
    if (!ui.key && !(await createProject())) return false;
    ui.rendering = !(p && p.mix.current);
    drawTimeline();
    setBusy(1);
    let reply;
    try {
      reply = await api("GET", "mix.mp3", undefined, { raw: true });
    } finally {
      ui.rendering = false;
      setBusy(-1);
    }
    if (!(reply instanceof Response)) {
      say(reply.lines);
      drawTimeline();
      return false;
    }
    const blob = await reply.blob();
    if (ui.playing) stopPlaying();
    if (ui.mix.url) URL.revokeObjectURL(ui.mix.url);
    ui.mix = { url: URL.createObjectURL(blob), blob, state: reply.headers.get("X-Gout-Mix-State"),
               headMs: Number(reply.headers.get("X-Gout-Head-Ms") || 0) };
    $("audio").src = ui.mix.url;
    await refresh();  // the master row has its waveform now
    return true;
  }

  async function startPlaying(fromMs) {
    if (ui.playing) return;
    if (!(await loadMix())) return false;
    const audio = $("audio");
    const at = fromMs !== undefined ? fromMs : ui.playhead;
    await new Promise((resolve) => {
      if (audio.readyState >= 1) return resolve();
      audio.addEventListener("loadedmetadata", resolve, { once: true });
    });
    const end = (audio.duration * 1000) - ui.mix.headMs;
    if (at >= end - 50) {
      say(`play  ${fmtMs(at)} is past the end of the mix (${fmtMs(end)})`);
      return false;
    }
    audio.currentTime = (Math.max(0, at) + ui.mix.headMs) / 1000;
    try {
      await audio.play();
    } catch (e) {
      say(`error: the browser would not play: ${e.message}`);
      return false;
    }
    ui.playing = true;
    ui.playhead = at;
    tick();
    return true;
  }

  function stopPlaying() {
    const audio = $("audio");
    if (!ui.playing) return;
    audio.pause();
    ui.playing = false;
    ui.playhead = Math.max(0, audio.currentTime * 1000 - ui.mix.headMs);
    drawTimeline();
  }

  function togglePlay() {
    if (ui.playing) stopPlaying();
    else startPlaying();
  }

  function seek(deltaMs) {
    const audio = $("audio");
    if (ui.playing) {
      audio.currentTime = Math.max(0, audio.currentTime + deltaMs / 1000);
    } else {
      ui.playhead = Math.max(0, ui.playhead + deltaMs);
      drawTimeline();
    }
  }

  let lastDraw = 0;
  function tick(now) {
    if (!ui.playing) return;
    const audio = $("audio");
    ui.playhead = Math.max(0, audio.currentTime * 1000 - ui.mix.headMs);
    if (!now || now - lastDraw > 66) {
      lastDraw = now || 0;
      if ($("screen").hidden) drawTimeline();
    }
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ the fractal screen

  function openScreen(argv) {
    if (!window.GoutFractal) return say("fractal: not loaded");
    window.GoutFractal.open({
      argv: argv || [],
      say,
      api,
      playing: () => ui.playing,
      position: () => ui.playhead,
      length: () => Math.max(0, ($("audio").duration || 0) * 1000 - ui.mix.headMs),
      hasTracks: () => !!(ui.project && ui.project.tracks.length),
      togglePlay,
      startPlaying,
      seek,
      mixState: () => ui.mix.state,
      closed: () => { draw(); $("line").focus(); },
    });
  }

  // ------------------------------------------------------------------ downloads

  const INSTALL = {
    windows: {
      title: "gout for Windows 10 and 11",
      files: [["windows", "download for Windows"]],
      steps: [
        "Open the downloaded gout-…-windows-x64-setup.exe.",
        "If Windows says \"Windows protected your PC\", click More info, then Run anyway. gout is free and not signed with a paid certificate, so Windows asks once.",
        "Click through the installer. It installs for you only and needs no administrator.",
        "Open gout from the Start menu: a terminal opens in your gout folder with the first commands.",
      ],
      project: "Your project from here: unzip gout-preview.zip, then in gout's terminal: gout -p \"%USERPROFILE%\\Downloads\\gout-preview\"",
    },
    mac: {
      title: "gout for macOS",
      files: [["macos-arm64", "Apple silicon (M1 and later)"], ["macos-x86_64", "Intel"]],
      steps: [
        "Open the downloaded pkg. macOS says it cannot verify the developer: click Done. gout is free and not signed with a paid certificate, so macOS asks once.",
        "Open System Settings → Privacy & Security, scroll down to the message about gout, click Open Anyway, then Open, and give your password.",
        "Click through the installer.",
        "Open gout from Launchpad, or type gout in Terminal.",
      ],
      project: "Your project from here: unzip gout-preview.zip, then in Terminal: gout -p ~/Downloads/gout-preview",
    },
    linux: {
      title: "gout for Linux (Ubuntu, Debian, Mint, Pop!_OS)",
      files: [["linux", "download the .deb"]],
      steps: [
        "Open the downloaded gout_…_all.deb: Ubuntu's App Center installs it. Or, in its folder: sudo apt install ./gout_*_all.deb",
        "apt brings Python and ffmpeg along.",
        "Open gout from your applications, or type gout in a terminal.",
        "Another Linux: run gout from its source (the README on GitHub); it needs Python 3.9 and ffmpeg.",
      ],
      project: "Your project from here: unzip gout-preview.zip, then: gout -p ~/Downloads/gout-preview",
    },
  };

  function showDownload(os) {
    const box = $("download-help");
    const guide = INSTALL[os];
    const found = ui.info.installers || {};
    const el = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    };
    box.replaceChildren(el("p", "", guide.title + (found.version ? ` (${found.version})` : "")));
    const row = el("p", "files");
    let any = false;
    for (const [key, label] of guide.files) {
      const file = found[key];
      if (!file) continue;
      any = true;
      const a = el("a", "button", `${label} · ${size(file.bytes)}`);
      a.href = file.url;
      a.rel = "noopener";
      a.title = file.name;
      row.appendChild(a);
    }
    if (any) {
      box.appendChild(row);
    } else {
      const none = el("p", "dim", "The installers are not published yet. ");
      if (found.page) {
        const a = el("a", "", "They will be on GitHub.");
        a.href = found.page;
        a.rel = "noopener";
        none.appendChild(a);
      }
      box.appendChild(none);
    }
    const list = el("ol");
    for (const step of guide.steps) list.appendChild(el("li", "", step));
    box.appendChild(list);
    box.appendChild(el("p", "dim", guide.project));
    box.hidden = false;
    for (const b of document.querySelectorAll(".button.os")) b.setAttribute("aria-pressed", String(b.dataset.os === os));
  }

  function guessOs() {
    const agent = navigator.userAgent || "";
    if (/Windows/i.test(agent)) return "windows";
    if (/Mac OS X|Macintosh/i.test(agent) && !/iPhone|iPad/i.test(agent)) return "mac";
    if (/Linux|X11/i.test(agent) && !/Android/i.test(agent)) return "linux";
    return null;
  }

  // ------------------------------------------------------------------ keys

  function onKey(e) {
    if (!$("screen").hidden) return;  // the fractal takes the keys while it is open
    const input = $("line");
    const inField = e.target === input;
    const empty = input.value === "";
    if (e.target !== input && e.target.closest && e.target.closest("button, a, input, label")) {
      if (!(e.ctrlKey || e.altKey)) return;  // buttons keep enter and space
    }
    const key = e.key;
    if (e.ctrlKey && (key === " " || e.code === "Space")) {
      e.preventDefault();
      return openScreen([]);
    }
    if (e.ctrlKey && !e.shiftKey && !e.altKey) {
      const k = key.toLowerCase();
      if (k === "u") { e.preventDefault(); return runLine("undo"); }
      if (k === "k") { e.preventDefault(); return toggle("cheat"); }
      if (k === "g") { e.preventDefault(); return togglePanel(); }
      if (k === "l") { e.preventDefault(); ui.log = []; return drawLog(); }
    }
    if (e.altKey && !e.ctrlKey && (key.toLowerCase() === "t" || e.code === "KeyT")) {
      e.preventDefault();
      return toggle("timeline");
    }
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (!inField) {
      if (key.length === 1 || key === "Enter" || key === "ArrowUp" || key === "ArrowDown") input.focus();
      if (key.length === 1 && key !== " ") return;  // the character lands in the line
    }
    if (key === "Enter") {
      e.preventDefault();
      submit();
    } else if (key === " " && empty) {
      e.preventDefault();
      togglePlay();
    } else if ((key === "ArrowLeft" || key === "ArrowRight") && empty) {
      e.preventDefault();
      seek(key === "ArrowLeft" ? -5000 : 5000);
    } else if (key === "ArrowUp" || key === "ArrowDown") {
      e.preventDefault();
      if (!ui.history.length) return;
      if (ui.historyAt === -1) {
        if (key === "ArrowDown") return;
        ui.draft = input.value;
        ui.historyAt = ui.history.length;
      }
      ui.historyAt += key === "ArrowUp" ? -1 : 1;
      if (ui.historyAt < 0) ui.historyAt = 0;
      if (ui.historyAt >= ui.history.length) {
        ui.historyAt = -1;
        input.value = ui.draft;
      } else {
        input.value = ui.history[ui.historyAt];
      }
      const end = input.value.length;
      input.setSelectionRange(end, end);
    } else if (key === "Tab") {
      e.preventDefault();
      if (empty) $("cheat").scrollTop += $("cheat").clientHeight - 20;
      else complete();
    } else if (key === "Escape") {
      input.value = "";
      ui.historyAt = -1;
    } else if (key === "PageUp" || key === "PageDown") {
      e.preventDefault();
      $("scroll").scrollTop += (key === "PageUp" ? -1 : 1) * ($("scroll").clientHeight - 30);
    }
  }

  // ------------------------------------------------------------------ start

  async function start() {
    measure();
    try {
      ui.history = JSON.parse(saved.get(HISTORY) || "[]").filter((l) => typeof l === "string");
    } catch (e) {
      ui.history = [];
    }
    try {
      const prefs = JSON.parse(saved.get(PREFS) || "{}");
      ui.showTimeline = prefs.timeline !== false;
      ui.showCheat = prefs.cheat !== false;
      ui.pictures = prefs.pictures !== false;
    } catch (e) { /* defaults */ }

    document.addEventListener("keydown", onKey);
    $("upload").addEventListener("change", (e) => {
      const files = e.target.files;
      if (files && files.length) uploadFiles(files).then(() => { e.target.value = ""; $("line").focus(); });
    });
    $("download-zip").addEventListener("click", downloadZip);
    $("download-mp3").addEventListener("click", downloadMp3);
    $("delete-project").addEventListener("click", deleteProject);
    $("big-play").addEventListener("click", () => openScreen([]));
    for (const b of document.querySelectorAll(".button.os")) b.addEventListener("click", () => showDownload(b.dataset.os));
    const mine = guessOs();
    if (mine) document.querySelector(`.button.os[data-os="${mine}"]`).classList.add("mine");
    $("left").addEventListener("click", () => { if (!window.getSelection().toString()) $("line").focus(); });
    $("audio").addEventListener("ended", () => {
      ui.playing = false;
      ui.playhead = 0;  // played to the end: back to the start, as in the ui
      drawTimeline();
    });

    let dragging = 0;
    window.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { dragging++; $("drop").hidden = false; } });
    window.addEventListener("dragleave", () => { dragging = Math.max(0, dragging - 1); if (!dragging) $("drop").hidden = true; });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      dragging = 0;
      $("drop").hidden = true;
      if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    });

    let resizing = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizing);
      resizing = setTimeout(() => { measure(); refresh(); loadCheat(); }, 200);
    });

    try {
      ui.info = await (await fetch("api/info", { cache: "no-store" })).json();
    } catch (e) {
      say("error: the server cannot be reached");
    }
    drawFiles();
    loadCheat();
    ui.key = saved.get(KEY);
    if (ui.key) {
      say([`gout preview ${ui.info.version || ""}: your project, as you left it. help shows the commands.`]);
      await refresh();
    } else {
      await createProject();
    }
    $("line").focus();
  }

  window.GoutPage = { ui, say, refresh };
  start();
})();
