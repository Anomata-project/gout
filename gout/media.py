"""ffmpeg and ffprobe: probing, cutting, mp3 frames, envelopes, spectra, loudness."""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from array import array

from .core import (
    CELL_AUDIBLE,
    die,
    fmt_size,
    fmt_time,
    run_quiet,
    SIZE_ACCEPT,
    SIZE_MAX_PASSES,
    TAG_ALLOWANCE,
)


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,bit_rate,channels,sample_rate:format=duration,bit_rate,format_name",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die(f"ffprobe failed on {path}:\n{out.stderr.strip()}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        die(f"no audio stream found in {path}")
    fmt = data.get("format") or {}

    duration = float(fmt.get("duration") or 0.0)
    if duration <= 0:
        die(f"could not determine the duration of {path}")

    bitrate = 0.0
    for candidate in (streams[0].get("bit_rate"), fmt.get("bit_rate")):
        try:
            bitrate = float(candidate)
        except (TypeError, ValueError):
            continue
        if bitrate > 0:
            break
    if bitrate <= 0:  # last resort: average over the whole file
        bitrate = path.stat().st_size * 8 / duration

    return {
        "duration": duration,
        "bitrate": bitrate,
        "codec": streams[0].get("codec_name") or "?",
        "channels": int(streams[0].get("channels") or 0),
        "sample_rate": int(streams[0].get("sample_rate") or 0),
        "format": fmt.get("format_name") or "",
    }


ENV_RATE = 50    # peaks per second of audio


ENV_SR = 8000    # decode rate for the envelope: 160 samples per peak


LEVELS = "▁▂▃▄▅▆▇█"  # 6 dB per step, top step is -6 dBFS and up


def compute_envelope(path: Path) -> tuple[bytes, bytes]:
    """(peaks, wave) per 20 ms window from a mono 8-bit decode. peaks: the larger swing, 0..128.
    wave: two bytes a window, the highest point above zero and the lowest below it, 0..128 each,
    which is what a waveform picture needs. Empty when ffmpeg fails."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", str(ENV_SR), "-f", "u8", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        return b"", b""
    data, n = result.stdout, ENV_SR // ENV_RATE
    peaks, wave = bytearray(), bytearray()
    for i in range(0, len(data), n):
        chunk = data[i:i + n]  # max/min on bytes run in C, so this is quick even for hours
        up, down = max(0, max(chunk) - 128), max(0, 128 - min(chunk))
        peaks.append(max(up, down))
        wave += bytes((up, min(down, 128)))
    return bytes(peaks), bytes(wave)


SPEC_BANDS = 40     # log-spaced 20 Hz .. 20 kHz


SPEC_N = 4096       # fft size


SPEC_WINDOWS = 8    # windows spread over up to a minute from the middle of the file


SPEC_SR = 44100


SPEC_RANGE = 60.0   # dB below the loudest band that still shows


def fft(x: list[complex]) -> list[complex]:
    """In-place style radix-2 FFT, pure Python; len(x) must be a power of two."""
    n = len(x)
    y = list(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            y[i], y[j] = y[j], y[i]
    length = 2
    while length <= n:
        ang = -2 * math.pi / length
        wlen = complex(math.cos(ang), math.sin(ang))
        half = length // 2
        for i in range(0, n, length):
            w = 1 + 0j
            for k in range(i, i + half):
                u, v = y[k], y[k + half] * w
                y[k], y[k + half] = u + v, u - v
                w *= wlen
        length <<= 1
    return y


def compute_spectrum(path: Path, duration: float) -> bytes:
    """Average spectrum of a file as SPEC_BANDS bytes: 255 is the loudest band, 0 is
    SPEC_RANGE dB under it. A minute from the middle of the file, a few Hann windows."""
    start = max(0.0, duration / 2 - 30)
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", "60", "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-ar", str(SPEC_SR), "-f", "s16le", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        return b""
    data = array("h")
    data.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    if len(data) < SPEC_N:
        return b""
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * n / (SPEC_N - 1)) for n in range(SPEC_N)]
    edges = [20 * (1000 ** (k / SPEC_BANDS)) for k in range(SPEC_BANDS + 1)]
    bins = []
    for k in range(SPEC_BANDS):
        lo = max(1, int(edges[k] * SPEC_N / SPEC_SR))
        hi = max(lo + 1, int(edges[k + 1] * SPEC_N / SPEC_SR))
        bins.append((min(lo, SPEC_N // 2 - 1), min(hi, SPEC_N // 2)))
    power = [0.0] * SPEC_BANDS
    step = max(SPEC_N, (len(data) - SPEC_N) // max(1, SPEC_WINDOWS - 1))
    starts = list(range(0, len(data) - SPEC_N + 1, step))[:SPEC_WINDOWS]
    for s0 in starts:
        frame = [complex(data[s0 + n] * hann[n]) for n in range(SPEC_N)]
        spectrum = fft(frame)
        mags = [abs(v) ** 2 for v in spectrum[:SPEC_N // 2]]
        for k, (lo, hi) in enumerate(bins):
            power[k] += sum(mags[lo:hi]) / (hi - lo)
    db = [10 * math.log10(v / len(starts) + 1e-9) for v in power]
    top = max(db)
    return bytes(max(0, min(255, round((v - top + SPEC_RANGE) / SPEC_RANGE * 255))) for v in db)


def level_char(peak: int) -> str:
    if peak <= 0:
        return LEVELS[0]
    db = 20 * math.log10(min(peak, 128) / 128)
    return LEVELS[max(0, min(7, 7 + math.ceil(db / 6)))]


def envelope_char(env: bytes, lo_ms: float, hi_ms: float) -> str:
    """The block for the loudest peak between two file times; █ when no envelope exists."""
    if not env:
        return CELL_AUDIBLE
    i0 = max(0, min(len(env) - 1, int(lo_ms * ENV_RATE / 1000)))
    i1 = max(i0 + 1, min(len(env), math.ceil(hi_ms * ENV_RATE / 1000)))
    return level_char(max(env[i0:i1]))


def cut(src: Path, dst: Path, start: float, duration: float | None,
        reencode: bool, bitrate: float, verbose: bool, codec: str = "mp3") -> None:
    """Write [start, start+duration) of src to dst.

    mp3 is stream-copied (frame-accurate) unless reencode; wav is rewritten with
    its own pcm codec, which is lossless and sample-accurate.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:a:0", "-map_metadata", "0"]
    if codec.startswith("pcm_"):
        cmd += ["-c:a", codec]
    else:
        cmd += ["-id3v2_version", "3", "-write_xing", "1"]
        if reencode:
            cmd += ["-c:a", "libmp3lame", "-b:a", f"{max(32, round(bitrate / 1000))}k"]
        else:
            cmd += ["-c:a", "copy"]
    cmd.append(str(dst))
    run_quiet(cmd, verbose)


def cut_to_size(src: Path, dst: Path, start: float, limit: int, remaining: float,
                reencode: bool, bitrate: float, verbose: bool) -> tuple[float, int]:
    """Cut the longest piece from `start` that still fits in `limit` bytes."""
    guess = max(0.05, (limit - TAG_ALLOWANCE) * 8 / bitrate)
    duration = min(guess, remaining)
    best: tuple[float, int] | None = None
    last = duration

    for attempt in range(1, SIZE_MAX_PASSES + 1):
        cut(src, dst, start, duration, reencode, bitrate, verbose)
        size = dst.stat().st_size
        last = duration
        if verbose:
            print(f"  pass {attempt}: {fmt_time(duration)} -> {fmt_size(size)}"
                  f" ({size / limit:.1%} of limit)", file=sys.stderr)

        if size <= limit and (best is None or duration > best[0]):
            best = (duration, size)
        if limit * SIZE_ACCEPT <= size <= limit:
            break
        if size <= limit and duration >= remaining - 1e-3:
            break  # already taking everything that is left

        scaled = min(duration * (limit * 0.998) / size, remaining)
        if abs(scaled - duration) < 0.05:
            break
        duration = max(0.05, scaled)

    if best is None:
        die(f"cannot fit anything into {fmt_size(limit)} — try a bigger --file-size")
    if abs(best[0] - last) > 1e-6:  # the last pass was not the keeper, redo it
        cut(src, dst, start, best[0], reencode, bitrate, verbose)
        best = (best[0], dst.stat().st_size)
    return best


#
# ffmpeg's own -ss on an mp3 with -c copy lands 90-115 ms late (measured, any length,
# CBR or VBR), which is too sloppy to keep a hard-trimmed track aligned. So hard trims
# of mp3 walk the frames in Python and copy whole frames: the removed head is then a
# known number of frames, and the ffmpeg pass afterwards only adds the Xing header and
# carries the tags. What ffmpeg's decoder skips at the start of a file depends on the
# LAME tag: delay+529 samples when the tag exists, nothing otherwise; the new file gets
# a tag with delay 0, so the audio moves by exactly the old delay (or -529 samples).
MP3_KBPS = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),  # MPEG-1 layer III
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),      # MPEG-2 / 2.5
}


MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


MP3_DECODER_DELAY = 529


def id3v2_size(data: bytes) -> int:
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    return 10 + size + (10 if data[5] & 0x10 else 0)


def mp3_lame_delay(data: bytes) -> int:
    """Encoder delay from the LAME/Lavc/Lavf tag in the Xing/Info frame, or -1 without one."""
    head = data[id3v2_size(data):][:4096]
    for tag in (b"Xing", b"Info"):
        i = head.find(tag)
        if i < 0:
            continue
        flags = int.from_bytes(head[i + 4:i + 8], "big")
        j = i + 8 + (4 if flags & 1 else 0) + (4 if flags & 2 else 0) \
            + (100 if flags & 4 else 0) + (4 if flags & 8 else 0)
        b = head[j + 21:j + 24]
        if len(b) == 3 and head[j:j + 4] in (b"LAME", b"Lavc", b"Lavf"):
            return (b[0] << 4) | (b[1] >> 4)
        return -1
    return -1


def mp3_index(data: bytes) -> tuple[array, array, int, int]:
    """Walk the layer III frames: (positions, sizes, sample_rate, samples_per_frame).

    ID3 tags and the Xing/Info/VBRI frame are skipped; junk is scanned past.
    """
    pos, end = id3v2_size(data), len(data)
    if end >= 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128
    positions, sizes = array("Q"), array("H")
    sr = spf = 0
    while pos + 4 <= end:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        b1, b2 = data[pos + 1], data[pos + 2]
        version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        bri, sri, pad = b2 >> 4, (b2 >> 2) & 3, (b2 >> 1) & 1
        if version == 1 or layer != 1 or bri in (0, 15) or sri == 3:
            pos += 1
            continue
        rate = MP3_RATES[version][sri]
        this_spf = 1152 if version == 3 else 576
        size = this_spf // 8 * MP3_KBPS[1 if version == 3 else 2][bri] * 1000 // rate + pad
        if pos + size > end:
            break
        if not sr:
            sr, spf = rate, this_spf
        if not positions:
            frame = data[pos:pos + size]
            if b"Xing" in frame or b"Info" in frame or b"VBRI" in frame:
                pos += size  # the VBR header frame carries no audio
                continue
        positions.append(pos)
        sizes.append(size)
        pos += size
    return positions, sizes, sr, spf


def mp3_frame_cut(src: Path, dst: Path, a_ms: int, b_ms: int, verbose: bool) -> tuple[int, int, int]:
    """Copy the frames covering [a_ms, b_ms) of src to dst without re-encoding.

    Returns (head_ms, kept_from_ms, kept_to_ms): head_ms is how much decoded audio
    disappeared from the front, so the track's timeline offset must grow by it.
    """
    data = src.read_bytes()
    positions, sizes, sr, spf = mp3_index(data)
    if len(positions) < 2:
        die(f"could not read the mp3 frames of {src.name}")
    frame_ms = spf * 1000 / sr
    k0 = max(0, min(len(positions) - 1, int(a_ms / frame_ms)))
    k1 = max(k0 + 1, min(len(positions), math.ceil(b_ms / frame_ms)))
    raw = dst.with_name(dst.name + ".frames")
    span = positions[k1 - 1] + sizes[k1 - 1] - positions[k0]
    if span == sum(sizes[k0:k1]):  # contiguous, the normal case
        raw.write_bytes(data[positions[k0]:positions[k0] + span])
    else:
        with raw.open("wb") as fh:
            for k in range(k0, k1):
                fh.write(data[positions[k]:positions[k] + sizes[k]])
    try:
        run_quiet(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                   "-f", "mp3", "-i", str(raw), "-map", "1:a", "-map_metadata", "0",
                   "-c:a", "copy", "-write_xing", "1", "-id3v2_version", "3", str(dst)], verbose)
    finally:
        raw.unlink(missing_ok=True)
    delay = mp3_lame_delay(data)
    # decoded_cut(y) == decoded_src(y + head): the old file skipped delay+529 samples
    # (or none without a tag), the new one skips 529, so the head is a bit less than k0 frames
    head = k0 * spf - (delay if delay >= 0 else -MP3_DECODER_DELAY)
    return round(head * 1000 / sr), round(k0 * frame_ms), round(k1 * frame_ms)


def write_float_wav(path: Path, chans: list[array], rate: int) -> None:
    frames = array("f", bytes(4 * len(chans[0]) * len(chans)))
    for c, data in enumerate(chans):
        frames[c::len(chans)] = data
    if sys.byteorder == "big":
        frames.byteswap()
    body = frames.tobytes()
    ch = len(chans)
    header = (b"RIFF" + (36 + len(body)).to_bytes(4, "little") + b"WAVE"
              + b"fmt " + (16).to_bytes(4, "little") + (3).to_bytes(2, "little") + ch.to_bytes(2, "little")
              + rate.to_bytes(4, "little") + (rate * ch * 4).to_bytes(4, "little")
              + (ch * 4).to_bytes(2, "little") + (32).to_bytes(2, "little")
              + b"data" + len(body).to_bytes(4, "little"))
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(header + body)
    tmp.replace(path)


def measure_loudness(path: Path, target: float = -23.0, ceiling: float = -1.0) -> dict | None:
    """Integrated loudness, true peak, LRA and threshold as loudnorm's first pass sees them."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"loudnorm=I={target}:TP={ceiling}:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
    if result.returncode != 0 or not match:
        return None
    data = json.loads(match.group(0))
    return {"i": float(data["input_i"]), "tp": float(data["input_tp"]), "lra": float(data["input_lra"]),
            "thresh": float(data["input_thresh"]), "offset": float(data.get("target_offset", 0))}


def fmt_lufs(m: dict | None) -> str:
    if not m or m["i"] == float("-inf") or m["i"] < -70:
        return "silent"
    return f"{m['i']:.1f} LUFS  LRA {m['lra']:.1f}  peak {m['tp']:+.1f} dBTP"
