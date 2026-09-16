"""Turning tracks into ffmpeg filtergraphs and rendering master.wav."""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

from .core import die, fmt_ms, fmt_size, GoutError, MASTER_MP3, MASTER_WAV, pan_filter, run_quiet
from .media import fmt_lufs, measure_loudness, probe
from .model import audible, CROSS_MS, is_heard, meets, part_label, part_owner, part_start, timeline
from .fx import Effect, FxContext, effect
from .settings import BITS_CODEC, master_track, setting, tag_args


def active_effects(project: "Project", t: dict, warnings: set[str] | None = None,
                   strict: bool = True) -> list[tuple[Effect, dict, FxContext]]:
    """The effects of a track (or the master) that will run, in order, with their settings.

    Bypassed ones are skipped; unknown kinds (a missing addon) and unreadable settings are
    skipped with a warning. An effect whose check fails stops the mix when strict, because
    a render that silently drops a delay is worse than an error."""
    ctx = FxContext(project, t)
    who = t.get("label") or t.get("name", "master")
    out = []
    for item in t.get("fx", []):
        if not item["on"]:
            continue
        eff = effect(item["kind"])
        if eff is None:
            if warnings is not None:
                warnings.add(f"{who}: {item['kind']} is not installed (an addon?), left out of the mix")
            continue
        try:
            params = eff.read(item["params"])
        except ValueError as exc:
            if warnings is not None:
                warnings.add(f"{who}: {item['kind']} {item['params']!r} is not valid ({exc}), left out")
            continue
        try:
            eff.check(ctx, params)
        except GoutError:
            if strict:
                raise
            continue
        out.append((eff, params, ctx))
    return out


def effect_tail_ms(project: "Project", t: dict) -> int:
    """How far a track's effects ring on after its audio ends: each effect adds its own tail."""
    return sum(eff.tail_ms(ctx, params) for eff, params, ctx in active_effects(project, t, strict=False))


def part_as_owner(t: dict, part: dict) -> dict:
    """A part in the shape of a track, for its effect chain: the track's file, the part's effects."""
    label = part_label(t["parts"], part)
    return {**t, "owner": part_owner(t["file"], part["id"]), "fx": part.get("fx", []), "part": part,
            "spec": f"{t['n']} {label}", "label": f"{t['name']} {label}"}


def sounding_end(project: "Project", t: dict) -> int:
    """Where a track stops making sound on the timeline, effect tails included (a part's too)."""
    end = timeline(t)[1]
    for part in t.get("parts") or []:
        if part.get("fx") and not part["mute"]:
            end = max(end, part_start(t, part) + part["out_ms"] - part["in_ms"]
                      + effect_tail_ms(project, part_as_owner(t, part)))
    return end + effect_tail_ms(project, t)


def chain_graph(project: "Project", t: dict, pre: list[str], post: list[str], src: str, out: str,
                inputs: list[Path], warnings: set[str] | None = None) -> str:
    """Filtergraph from `src` through `pre`, the effects in order, and `post`, to [out]."""
    parts: list[str] = []
    cur = src
    filters = list(pre)
    for k, (eff, params, ctx) in enumerate(active_effects(project, t, warnings)):
        if eff.uses_graph():
            parts.append(f"{cur}{','.join(filters) or 'anull'}[{out}p{k}]")
            parts.append(eff.graph(ctx, params, f"[{out}p{k}]", f"{out}q{k}", inputs))
            cur, filters = f"[{out}q{k}]", []
        else:
            filters += eff.filters(ctx, params)
    parts.append(f"{cur}{','.join(filters + post) or 'anull'}[{out}]")
    return ";".join(parts)


def track_head(project: "Project", t: dict, shift_ms: int = 0) -> list[str] | None:
    """The filters that put a track on the timeline in stereo: format, soft trim, position.
    shift_ms starts the timeline that late (live playback from the playhead): what comes before
    is trimmed off here, before any effect sees it. None when nothing of it is audible."""
    a, b = audible(t)
    start = t["offset_ms"] + a - shift_ms
    trim_start, delay = a, start
    if start < 0:  # the head hangs before the timeline: cut it, no delay
        trim_start, delay = a - start, 0
    if b <= trim_start:
        return None
    steps = [f"aformat=sample_rates={project.rate}:sample_fmts=fltp"]
    if trim_start > 0 or b < t["length_ms"]:
        steps.append(f"atrim=start={trim_start / 1000:.3f}:end={b / 1000:.3f}")
        steps.append("asetpts=PTS-STARTPTS")
    if delay > 0:
        steps.append(f"adelay={delay}:all=1")
    if t["channels"] != 2:
        steps.append(pan_filter(t["channels"], 0.0))  # effects always see stereo
    return steps


def part_heads(project: "Project", t: dict, shift_ms: int = 0) -> list[tuple[dict, list[str]]]:
    """The filters that put each heard part of a split track on the timeline, like track_head,
    counted in samples at the project rate. Where two parts meet, each reaches CROSS_MS over
    the cut with a linear fade, so the two add up to the unbroken sound when they are alike
    (measured: -162 dB) and a change of gain between them does not click."""
    rate, length = project.rate, t["length_ms"]
    at = lambda ms: round(ms * rate / 1000)
    parts = t["parts"]
    cross = at(CROSS_MS)
    out = []
    first_in, last_out = audible(t)
    for k, part in enumerate(parts):
        lo, hi = at(part["in_ms"]), at(min(part["out_ms"], length))
        before = after = 0
        fade_in = fade_out = 0  # a cut edge left on its own (moved, trimmed, a neighbour removed) fades
        prev = next((p for p in parts if meets(p, part)), None)
        nxt = next((p for p in parts if meets(part, p)), None)
        if prev is not None:
            before = min(cross, (hi - lo) // 2, (lo - at(prev["in_ms"])) // 2, lo)
        elif part["in_ms"] > first_in:
            fade_in = min(cross, (hi - lo) // 2)
        if nxt is not None:
            after = min(cross, (hi - lo) // 2, (at(min(nxt["out_ms"], length)) - hi) // 2, at(length) - hi)
        elif part["out_ms"] < last_out:
            fade_out = min(cross, (hi - lo) // 2)
        if part["mute"] or hi <= lo:
            continue
        start = at(t["offset_ms"] + part["shift_ms"]) + lo - before - at(shift_ms)
        size = hi + after - (lo - before)
        steps = [f"aformat=sample_rates={rate}:sample_fmts=fltp",
                 f"atrim=start_sample={lo - before}:end_sample={hi + after}", "asetpts=PTS-STARTPTS"]
        if before or fade_in:
            steps.append(f"afade=t=in:ss=0:ns={2 * before or fade_in}:curve=tri")
        if after or fade_out:
            steps.append(f"afade=t=out:ss={size - (2 * after or fade_out)}:ns={2 * after or fade_out}:curve=tri")
        if start < 0:  # live playback from inside or after the part: cut what comes before the playhead
            if -start >= size:
                continue
            steps += [f"atrim=start_sample={-start}", "asetpts=PTS-STARTPTS"]
            start = 0
        if start > 0:
            steps.append(f"adelay={start}S:all=1")
        if t["channels"] != 2:
            steps.append(pan_filter(t["channels"], 0.0))
        out.append((part, steps))
    return out


def sounds(project: "Project", t: dict, shift_ms: int = 0) -> bool:
    """Whether anything of a track reaches the mix from shift_ms on (mute and solo aside)."""
    if t.get("parts"):
        return bool(part_heads(project, t, shift_ms))
    return track_head(project, t, shift_ms) is not None


def track_chain(project: "Project", t: dict, src: str, out: str, inputs: list[Path],
                warnings: set[str] | None = None, shift_ms: int = 0) -> str | None:
    """One track's whole filtergraph from its input label to [out]: position, effects in
    order, then gain and pan like a channel strip's fader. A track in parts puts each part in
    place with its own gain and pan first and sums them. None when nothing is audible."""
    post = []
    if t["gain_db"]:
        post.append(f"volume={t['gain_db']:.2f}dB")
    if abs(t["pan"]) >= 0.005:
        post.append(pan_filter(2, t["pan"]))
    if not t.get("parts"):
        head = track_head(project, t, shift_ms)
        if head is None:
            return None
        return chain_graph(project, t, head, post, src, out, inputs, warnings)
    heads = part_heads(project, t, shift_ms)
    if not heads:
        return None
    def part_graph(part: dict, steps: list[str], source: str, label: str) -> str:
        """A part through its effects, then its own gain and pan."""
        own = []
        if part["gain_db"]:
            own.append(f"volume={part['gain_db']:.2f}dB")
        if abs(part["pan"]) >= 0.005:
            own.append(pan_filter(2, part["pan"]))
        return chain_graph(project, part_as_owner(t, part), steps, own, source, label, inputs, warnings)

    graph = []
    if len(heads) == 1:
        graph.append(part_graph(*heads[0], src, f"{out}m"))
    else:
        graph.append(f"{src}asplit={len(heads)}" + "".join(f"[{out}s{k}]" for k in range(len(heads))))
        graph += [part_graph(part, steps, f"[{out}s{k}]", f"{out}r{k}") for k, (part, steps) in enumerate(heads)]
        graph.append("".join(f"[{out}r{k}]" for k in range(len(heads)))  # r: not the q labels chain_graph makes
                     + f"amix=inputs={len(heads)}:normalize=0:duration=longest:dropout_transition=0[{out}m]")
    return ";".join(graph + [chain_graph(project, t, [], post, f"[{out}m]", out, inputs, warnings)])


def build_graph(project: "Project", tracks: list[dict], warnings: set[str] | None = None,
                shift_ms: int = 0) -> tuple[list[Path], str, list[dict]]:
    any_solo = any(t["solo"] for t in tracks)
    used = [t for t in tracks if is_heard(t, any_solo) and sounds(project, t, shift_ms)]
    if not used:
        return [], "", []
    inputs: list[Path] = [project.tracks_dir / t["file"] for t in used]  # effect files follow
    chains = [track_chain(project, t, f"[{i}:a]", f"t{i}", inputs, warnings, shift_ms) for i, t in enumerate(used)]
    labels = "".join(f"[t{i}]" for i in range(len(used)))
    chains.append(f"{labels}amix=inputs={len(used)}:normalize=0:duration=longest"
                  f":dropout_transition=0[mix]")
    return inputs, ";".join(chains), used


def ebur128_summary(stderr: str) -> dict | None:
    """Integrated loudness, loudness range and true peak from ffmpeg's ebur128 summary."""
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else ""
    found = {key: re.search(pattern, summary) for key, pattern in
             (("i", r"I:\s+(-?[\d.]+|-inf) LUFS"), ("lra", r"LRA:\s+(-?[\d.]+) LU"), ("tp", r"Peak:\s+(-?[\d.]+|-inf) dBFS"))}
    if not all(found.values()):
        return None
    return {key: float(m.group(1)) for key, m in found.items()}


def mix(project: Project, verbose: bool = False, mp3: bool = False) -> None:
    tracks = project.tracks()
    state = project.state_fingerprint()  # what this render will sound like, for play to compare
    warnings: set[str] = set()
    inputs, graph, used = build_graph(project, tracks, warnings)
    if not inputs:
        if project.master.exists():
            project.master.unlink()
        for key in ("master_ms", "master_lufs", "master_tp", "master_lra", "master_state"):
            project.unset(key)
        print("mix   nothing audible" + (" — master.wav removed" if tracks else "")
              + ("" if tracks else " (no tracks yet)"))
        return

    # pass 1: the sum, the master's effects, master gain and fades, float at the project rate
    master = master_track(project)
    after: list[str] = []
    gain = float(setting(project, "gain"))
    if gain:
        after.append(f"volume={gain:.2f}dB")
    fade_in, fade_out = int(setting(project, "fadein")), int(setting(project, "fadeout"))
    end_ms = max(sounding_end(project, t) for t in used) + effect_tail_ms(project, master)
    if fade_in > 0:
        after.append(f"afade=t=in:d={fade_in / 1000:.3f}")
    if fade_out > 0:
        after.append(f"afade=t=out:st={max(0, end_ms - fade_out) / 1000:.3f}:d={fade_out / 1000:.3f}")
    graph = graph[:-len("[mix]")] + "[sum];" + chain_graph(project, master, [], after, "[sum]", "mix", inputs, warnings)
    raw = project.root / "master.raw.part.wav"
    final = project.root / "master.part.wav"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", "[mix]", "-ar", str(project.rate), "-c:a", "pcm_f32le", str(raw)]
    how = ""
    target = None
    down = 0  # dB turned down before loudnorm
    try:
        run_quiet(cmd, verbose)

        # pass 2: loudness, padding, format and tags
        lufs = setting(project, "lufs")
        target = None if lufs == "off" else float(lufs)
        ceiling = float(setting(project, "ceiling"))
        # only a loudness target needs the render measured before the final pass
        measured = measure_loudness(raw, target, ceiling) if target is not None else None
        duration = probe(raw)["duration"] if target is not None else 0.0
        chain: list[str] = []
        if target is not None and measured is None:
            how = "loudness could not be measured, left as is"
        elif target is not None:
            if measured["i"] < -70:
                how = "silent, nothing to normalise"
            elif duration < 3:
                g = min(target - measured["i"], ceiling - measured["tp"])
                chain.append(f"volume={g:.2f}dB")
                how = f"gain {g:+.1f} dB (plain gain: under 3 s)"
            else:
                lra = max(7, min(50, math.ceil(measured["lra"]) + 1))
                # loudnorm takes a measured loudness and threshold up to 0 LUFS only: a sum louder than
                # that (float, so nothing clipped) is turned down first, and its measurements with it
                down = max(0, math.ceil(max(measured["i"], measured["thresh"]) + 1))
                if down:
                    chain.append(f"volume=-{down}dB")
                # loudnorm reads a measured LRA of exactly 0 as "unknown" and refuses linear mode
                chain.append(f"loudnorm=I={target}:TP={ceiling}:LRA={lra}:measured_I={measured['i'] - down}"
                             f":measured_TP={measured['tp'] - down}:measured_LRA={max(measured['lra'], 0.01)}"
                             f":measured_thresh={measured['thresh'] - down}:offset={measured['offset']}"
                             f":linear=true:print_format=json")
                how = "loudnorm"
        head, tail = int(setting(project, "head")), int(setting(project, "tail"))
        if head > 0:
            chain.append(f"adelay={head}:all=1")
        if tail > 0:
            chain.append(f"apad=pad_dur={tail / 1000:.3f}")
        bits = setting(project, "bits")
        chain.append("ebur128=peak=true:framelog=quiet")  # the mix line's numbers, measured while writing
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-y", "-i", str(raw)]
        if chain:
            cmd += ["-af", ",".join(chain)]
        cmd += ["-ar", str(project.rate)]
        if bits == "16":
            cmd += ["-dither_method", "triangular"]
        cmd += ["-c:a", BITS_CODEC[bits], *tag_args(project), str(final)]
        if verbose:
            print("  $ " + " ".join(cmd), file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            problems = [l for l in result.stderr.splitlines() if "rror" in l or "nvalid" in l]
            die("ffmpeg failed:\n" + "\n".join(problems[-12:]))
        if how == "loudnorm":
            match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
            info = json.loads(match.group(0)) if match else {}
            mode = info.get("normalization_type", "?")
            delta = float(info.get("output_i", 0)) - float(info.get("input_i", 0)) - down  # with the turn-down
            how = (f"linear gain {delta:+.1f} dB" if mode == "linear"
                   else f"dynamic: the ceiling stopped a plain gain of {target - measured['i']:+.1f} dB")
        final.replace(project.master)
    finally:
        for tmp in (raw, final):
            if tmp.exists():
                tmp.unlink()

    length_ms = round(probe(project.master)["duration"] * 1000)
    got = ebur128_summary(result.stderr)
    project.set("master_ms", str(length_ms))
    for key, field in (("master_lufs", "i"), ("master_tp", "tp"), ("master_lra", "lra")):
        project.set(key, "" if got is None else f"{got[field]:.2f}")
    project.envelope(MASTER_WAV, project.master)
    project.set("master_state", state)
    if target is not None and measured is not None and got is not None:
        project.set("master_norm_db", f"{got['i'] - measured['i']:.2f}")  # what live playback applies
    else:
        project.unset("master_norm_db")
    skipped = len(tracks) - len(used)
    note = f"  ({len(used)} of {len(tracks)} tracks)" if skipped else ""
    print(f"mix   {MASTER_WAV}  {fmt_ms(length_ms)}  {fmt_lufs(got)}{note}")
    if how:
        print(f"      {how}")
    for warning in sorted(warnings):
        print(f"      {warning}")
    if got and target is None and got["tp"] > 0:
        print("      true peak above 0 dBTP: it will clip on export — set lufs -14, or lower a gain")
    if mp3:
        out = project.root / MASTER_MP3
        quality = setting(project, "mp3")
        q = ["-q:a", quality[1:]] if quality.startswith("v") else ["-b:a", quality]
        run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(project.master),
                   "-c:a", "libmp3lame", *q, "-id3v2_version", "3", *tag_args(project), str(out)], verbose)
        print(f"      {MASTER_MP3}  {fmt_size(out.stat().st_size)}  ({quality})")


def live_source(project: "Project", from_ms: int, warnings: set[str] | None = None) -> tuple[list[str], int] | None:
    """ffmpeg arguments (inputs and filtergraph, mapped to one stream, no output) that play the
    project from from_ms without rendering it: the tracks, their effects, the master chain, gain
    and fades as in mix. A loudness target is applied as the gain the last render measured, and a
    limiter keeps peaks under -1 dBFS, since the real loudness step needs the whole mix.
    Returns (arguments, length of what will play in ms), or None when nothing is audible."""
    tracks = project.tracks()
    inputs, graph, used = build_graph(project, tracks, warnings, from_ms)
    if not inputs:
        return None
    master = master_track(project)
    end_ms = max(sounding_end(project, t) for t in used) + effect_tail_ms(project, master)
    after: list[str] = []
    gain = float(setting(project, "gain"))
    if gain:
        after.append(f"volume={gain:.2f}dB")
    fade_in, fade_out = int(setting(project, "fadein")), int(setting(project, "fadeout"))
    if fade_in > 0 and from_ms < fade_in:
        after.append(f"afade=t=in:st=0:d={(fade_in - from_ms) / 1000:.3f}")
    if fade_out > 0:
        start = max(0, end_ms - fade_out - from_ms)
        after.append(f"afade=t=out:st={start / 1000:.3f}:d={min(fade_out, end_ms - from_ms) / 1000:.3f}")
    norm = project.get("master_norm_db")
    if setting(project, "lufs") != "off" and norm:
        after.append(f"volume={float(norm):.2f}dB")
    after.append("alimiter=limit=0.891:level=false:latency=true")  # no lookahead delay on the playhead
    graph = graph[:-len("[mix]")] + "[sum];" + chain_graph(project, master, [], after, "[sum]", "live", inputs, warnings)
    args: list[str] = []
    for path in inputs:
        args += ["-i", str(path)]
    args += ["-filter_complex", graph, "-map", "[live]"]
    return args, max(0, end_ms - from_ms)


def autorender(project: Project, args: "Args") -> None:
    """Re-render master.wav after a change when autorender is on, unless -N was given."""
    if args.no_mix:
        return
    if project.autorender:
        mix(project)
