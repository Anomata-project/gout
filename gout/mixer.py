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
from .effects.eq import eq_filters, track_eq
from .effects.comp import comp_filter, track_comp
from .effects.delay import delay_filter, delay_taps, track_delay
from .effects.reverb import reverb_graph, reverb_tail_ms, track_reverb
from .settings import BITS_CODEC, master_track, project_bpm, setting, tag_args


def effect_tail_ms(project: "Project", t: dict) -> int:
    """How far a track's effects ring on after its audio ends."""
    tail = 0.0
    dly = track_delay(t)
    if dly is not None:
        try:
            taps = delay_taps(dly, project_bpm(project))
        except GoutError:
            taps = []
        if taps:
            tail = max(tail, taps[-1][0])
    rv = track_reverb(t)
    if rv is not None:
        tail += reverb_tail_ms(rv)  # a reverb after a delay rings on after its last repeat
    return math.ceil(tail)


def sounding_end(project: "Project", t: dict) -> int:
    """Where a track stops making sound on the timeline, effect tails included."""
    return timeline(t)[1] + effect_tail_ms(project, t)


def track_chain(project: "Project", t: dict, steps: list[str], src: str, out: str, inputs: list[Path]) -> str:
    """One track's whole filtergraph from its input label to [out], reverb included."""
    rv = track_reverb(t)
    if rv is None:
        return f"{src}{','.join(steps)}[{out}]"
    return f"{src}{','.join(steps)}[{out}p];" + reverb_graph(project, rv, f"[{out}p]", out, inputs)


def pan_filter(channels: int, pan: float) -> str:
    """Stereo output, centre by default: mono goes equally to L and R."""
    left = min(1.0, 1.0 - pan)
    right = min(1.0, 1.0 + pan)
    if channels == 1:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c0"
    if channels == 2:
        return f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"
    return f"aformat=channel_layouts=stereo,pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1"


def track_steps(project: Project, t: dict) -> list[str] | None:
    """The filters that put one track on the timeline as the mix hears it: format, soft
    trim, position, gain, pan. None when nothing of it is audible."""
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
    steps += eq_filters(track_eq(t))
    comp = track_comp(t)
    if comp is not None:
        steps.append(comp_filter(comp))
    dly = track_delay(t)
    if dly is not None:
        echo = delay_filter(dly, project_bpm(project))
        if echo:
            steps.append(echo)
    if t["gain_db"]:
        steps.append(f"volume={t['gain_db']:.2f}dB")
    steps.append(pan_filter(t["channels"], t["pan"]))
    return steps


def build_graph(project: Project, tracks: list[dict]) -> tuple[list[Path], str, list[dict]]:
    any_solo = any(t["solo"] for t in tracks)
    plans = []
    for t in tracks:
        if not is_heard(t, any_solo):
            continue
        steps = track_steps(project, t)
        if steps is not None:
            plans.append((t, steps))
    if not plans:
        return [], "", []
    inputs: list[Path] = [project.tracks_dir / t["file"] for t, _ in plans]  # reverb responses follow
    chains = [track_chain(project, t, steps, f"[{i}:a]", f"t{i}", inputs) for i, (t, steps) in enumerate(plans)]
    labels = "".join(f"[t{i}]" for i in range(len(plans)))
    chains.append(f"{labels}amix=inputs={len(plans)}:normalize=0:duration=longest"
                  f":dropout_transition=0[mix]")
    return inputs, ";".join(chains), [t for t, _ in plans]


def mix(project: Project, verbose: bool = False, mp3: bool = False) -> None:
    tracks = project.tracks()
    inputs, graph, used = build_graph(project, tracks)
    if not inputs:
        if project.master.exists():
            project.master.unlink()
        for key in ("master_ms", "master_lufs", "master_tp", "master_lra"):
            project.unset(key)
        print("mix   nothing audible" + (" — master.wav removed" if tracks else "")
              + ("" if tracks else " (no tracks yet)"))
        return

    # pass 1: the sum, master eq and compressor, master gain and fades, float at the project rate
    post: list[str] = []
    post += eq_filters(track_eq(master_track(project)))
    master_comp = track_comp(master_track(project))
    if master_comp is not None:
        post.append(comp_filter(master_comp))
    master_delay = track_delay(master_track(project))
    if master_delay is not None:
        echo = delay_filter(master_delay, project_bpm(project))
        if echo:
            post.append(echo)
    master_reverb = track_reverb(master_track(project))
    after: list[str] = []  # gain and fades come after the master reverb
    gain = float(setting(project, "gain"))
    if gain:
        after.append(f"volume={gain:.2f}dB")
    fade_in, fade_out = int(setting(project, "fadein")), int(setting(project, "fadeout"))
    end_ms = max(sounding_end(project, t) for t in used) + effect_tail_ms(project, master_track(project))
    if fade_in > 0:
        after.append(f"afade=t=in:d={fade_in / 1000:.3f}")
    if fade_out > 0:
        after.append(f"afade=t=out:st={max(0, end_ms - fade_out) / 1000:.3f}:d={fade_out / 1000:.3f}")
    if post or after or master_reverb is not None:
        graph = graph[:-len("[mix]")] + "[sum]"
        cur = "[sum]"
        if post:
            graph += f";{cur}{','.join(post)}[mpre]"
            cur = "[mpre]"
        if master_reverb is not None:
            graph += ";" + reverb_graph(project, master_reverb, cur, "mrev", inputs)
            cur = "[mrev]"
        graph += f";{cur}{','.join(after) if after else 'anull'}[mix]"
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
