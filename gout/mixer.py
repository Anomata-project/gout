"""Turning tracks into ffmpeg filtergraphs and rendering master.wav."""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

from .core import die, fmt_ms, fmt_size, GoutError, MASTER_MP3, MASTER_WAV, run_quiet
from .media import fmt_lufs, measure_loudness, probe
from .model import audible, is_heard, timeline
from .fx import Effect, FxContext, effect
from .settings import BITS_CODEC, master_track, setting, tag_args


def active_effects(project: "Project", t: dict, warnings: set[str] | None = None,
                   strict: bool = True) -> list[tuple[Effect, dict, FxContext]]:
    """The effects of a track (or the master) that will run, in order, with their settings.

    Bypassed ones are skipped; unknown kinds (a missing addon) and unreadable settings are
    skipped with a warning. An effect whose check fails stops the mix when strict, because
    a render that silently drops a delay is worse than an error."""
    ctx = FxContext(project, t)
    who = t.get("name", "master")
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


def sounding_end(project: "Project", t: dict) -> int:
    """Where a track stops making sound on the timeline, effect tails included."""
    return timeline(t)[1] + effect_tail_ms(project, t)


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


def pan_filter(channels: int, pan: float) -> str:
    """Stereo output, centre by default: mono goes equally to L and R."""
    left = min(1.0, 1.0 - pan)
    right = min(1.0, 1.0 + pan)
    if channels == 1:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c0"
    if channels == 2:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"
    return f"aformat=channel_layouts=stereo,pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"


def track_head(project: "Project", t: dict) -> list[str] | None:
    """The filters that put a track on the timeline in stereo: format, soft trim, position.
    None when nothing of it is audible."""
    a, b = audible(t)
    start = t["offset_ms"] + a
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


def track_chain(project: "Project", t: dict, src: str, out: str, inputs: list[Path],
                warnings: set[str] | None = None) -> str | None:
    """One track's whole filtergraph from its input label to [out]: position, effects in
    order, then gain and pan like a channel strip's fader. None when nothing is audible."""
    head = track_head(project, t)
    if head is None:
        return None
    post = []
    if t["gain_db"]:
        post.append(f"volume={t['gain_db']:.2f}dB")
    if abs(t["pan"]) >= 0.005:
        post.append(pan_filter(2, t["pan"]))
    return chain_graph(project, t, head, post, src, out, inputs, warnings)


def build_graph(project: "Project", tracks: list[dict], warnings: set[str] | None = None
                ) -> tuple[list[Path], str, list[dict]]:
    any_solo = any(t["solo"] for t in tracks)
    used = [t for t in tracks if is_heard(t, any_solo) and track_head(project, t) is not None]
    if not used:
        return [], "", []
    inputs: list[Path] = [project.tracks_dir / t["file"] for t in used]  # effect files follow
    chains = [track_chain(project, t, f"[{i}:a]", f"t{i}", inputs, warnings) for i, t in enumerate(used)]
    labels = "".join(f"[t{i}]" for i in range(len(used)))
    chains.append(f"{labels}amix=inputs={len(used)}:normalize=0:duration=longest"
                  f":dropout_transition=0[mix]")
    return inputs, ";".join(chains), used


def mix(project: Project, verbose: bool = False, mp3: bool = False) -> None:
    tracks = project.tracks()
    warnings: set[str] = set()
    inputs, graph, used = build_graph(project, tracks, warnings)
    if not inputs:
        if project.master.exists():
            project.master.unlink()
        for key in ("master_ms", "master_lufs", "master_tp", "master_lra"):
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
    try:
        run_quiet(cmd, verbose)

        # pass 2: loudness, padding, format and tags
        lufs = setting(project, "lufs")
        target = None if lufs == "off" else float(lufs)
        ceiling = float(setting(project, "ceiling"))
        measured = measure_loudness(raw, target if target is not None else -23.0, ceiling)
        duration = probe(raw)["duration"]
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
                # loudnorm reads a measured LRA of exactly 0 as "unknown" and refuses linear mode
                chain.append(f"loudnorm=I={target}:TP={ceiling}:LRA={lra}:measured_I={measured['i']}"
                             f":measured_TP={measured['tp']}:measured_LRA={max(measured['lra'], 0.01)}"
                             f":measured_thresh={measured['thresh']}:offset={measured['offset']}"
                             f":linear=true:print_format=json")
                how = "loudnorm"
        head, tail = int(setting(project, "head")), int(setting(project, "tail"))
        if head > 0:
            chain.append(f"adelay={head}:all=1")
        if tail > 0:
            chain.append(f"apad=pad_dur={tail / 1000:.3f}")
        bits = setting(project, "bits")
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-y", "-i", str(raw)]
        if chain:
            cmd += ["-af", ",".join(chain)]
        cmd += ["-ar", str(project.rate)]
        if bits == "16":
            cmd += ["-dither_method", "triangular"]
        cmd += ["-c:a", BITS_CODEC[bits], *tag_args(project), str(final)]
        if verbose:
            print("  $ " + " ".join(cmd), file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            problems = [l for l in result.stderr.splitlines() if "rror" in l or "nvalid" in l]
            die("ffmpeg failed:\n" + "\n".join(problems[-12:]))
        if how == "loudnorm":
            match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
            info = json.loads(match.group(0)) if match else {}
            mode = info.get("normalization_type", "?")
            delta = float(info.get("output_i", 0)) - float(info.get("input_i", 0))
            how = (f"linear gain {delta:+.1f} dB" if mode == "linear"
                   else f"dynamic: the ceiling stopped a plain gain of {target - measured['i']:+.1f} dB")
        final.replace(project.master)
    finally:
        for tmp in (raw, final):
            if tmp.exists():
                tmp.unlink()

    length_ms = round(probe(project.master)["duration"] * 1000)
    got = measure_loudness(project.master)
    project.set("master_ms", str(length_ms))
    for key, field in (("master_lufs", "i"), ("master_tp", "tp"), ("master_lra", "lra")):
        project.set(key, "" if got is None else f"{got[field]:.2f}")
    project.envelope(MASTER_WAV, project.master)
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


def autorender(project: Project, args: "Args") -> None:
    """Re-render master.wav after a change, unless -N was given or the setting is off."""
    if args.no_mix:
        return
    if project.autorender:
        mix(project)
