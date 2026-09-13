# gout

A command-line DAW. Stack `wav` and `mp3` tracks on a timeline, trim and move them, and mix
them to a stereo `master.wav`. The original cutter is still in there as `gout cut`.

Wraps `ffmpeg`. Everything else is the Python standard library: no dependencies, no virtualenv.
One file, `gout.py`.

## Install

Needs `ffmpeg` and `ffprobe` on `PATH`.

```sh
./install.sh              # symlinks ~/.local/bin/gout -> ./gout.py
```

The symlink points back at this checkout, so editing `gout.py` takes effect immediately.
`pipx install .` and `pip install --user -e .` work too, or run `./gout.py` in place.

## A project

```sh
gout new song && cd song
gout add drums.mp3 bass.wav          # copied into master/, master.wav mixed
gout add vocals.flac --at 00:00:08   # other formats are decoded to wav on the way in
gout move 3 +250ms                   # nudge later; -250ms earlier; 1:30 places it there
gout trim 3 -st 2s -et 1:40          # soft trim: in/out points, the file is untouched
gout trim 3 --hard                   # hard trim: bake it into the file, lighter material
gout solo 3 && gout mix --mp3        # hear one track, bounce master.mp3 as well
gout undo                            # every change is recorded
gout                                 # open the terminal ui
```

A project is a directory:

```
song/
  gout.db        sqlite: track order, positions, trims, gain, pan, mute/solo, undo history
  master/        the track files, wav or mp3, never modified by moves or soft trims
  master.wav     the mix, 32-bit float stereo at the project rate (48 kHz by default)
```

`gout.db` is the only thing that can get out of step with the files. `gout rebuild` recreates it
from whatever is in `master/`, every track at 0.

## Commands

Run them inside the project, or pass `-p DIR` first. `TRACK` is the number shown by `ls` or
the track name. `--no-mix` on any change skips the automatic re-mix; `gout set automix off`
turns it off for good.

| command | meaning |
| --- | --- |
| `new NAME [--rate HZ]` | create a project |
| `add FILE... [--name N] [--at TIME]` | add tracks at `TIME` (default 0) |
| `ls` | list tracks, positions, trims, flags |
| `view` | print the timeline once |
| `move TRACK +TIME \| -TIME \| TIME` | nudge later, nudge earlier, place at a time |
| `trim TRACK [-st T] [-et T \| -el T]` | soft trim, times count from the start of the track's file |
| `trim TRACK --clear` | soft trim off |
| `trim TRACK --hard [-st ..] [-et ..] [-r]` | rewrite the file; bakes the soft trim when no times given |
| `rm TRACK [-D]` | drop a track; `-D` also deletes its file |
| `mute TRACK [on\|off]`, `solo TRACK [on\|off]` | toggle; `mute all off`, `solo all off` |
| `gain TRACK DB`, `pan TRACK L30\|R30\|C` | mixer strip; every track starts at 0 dB, centred |
| `mix [--mp3] [-v]` | render `master.wav`; `--mp3` also writes `master.mp3` |
| `undo` | undo the last change (not a hard trim or `rm -D`) |
| `dump`, `rebuild [-f]` | state as JSON; recreate `gout.db` from `master/` |
| `set automix on\|off`, `set rate HZ` | project settings |
| `cut INPUT ...` | the 1.x cutter, see below |

## The terminal ui

`gout` inside a project (or `gout ui`) opens a split screen. The left side is a prompt that
takes the same commands without the leading `gout`, with a scrolling log above it. The right
side is the timeline, one row per track: `█` is the audible part, `░` is material that is
soft-trimmed away, `▒` a track that is muted or not soloed. It is a picture, not a mouse
target: the keyboard drives everything.

```
 gout song  48000 Hz  4 tracks  automix on │ timeline
 > add vocals.mp3                          │                0:00        0:30        1:00
 add    4  vocals  mp3  2ch ...            │                ┼───────────┼───────────┼──────
 mix   master.wav  00:02:01.000  peak -3.1 │  1 drums       ████████████████████████████
 > move 4 +1.5s                            │  2 bass        ████████████████████████████
 move   4  vocals  at 00:00:01.500 -> ...  │  3 gtr      M     ░░▒▒▒▒▒▒▒▒▒▒▒▒░░
 mix   master.wav  00:02:01.000  peak -3.1 │  4 vocals              ████████████████████
 > _                                       │    master.wav  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

`ctrl-u` or `view` hides and shows the timeline, `help` lists the commands, `help all` prints
the whole instruction page, `quit` / `ctrl-d` / `ctrl-c` leave.

## How the mix works

Every track is decoded, trimmed to its in/out points, delayed to its position, panned to
stereo (mono goes equally to L and R) and summed with ffmpeg's `amix` with normalisation off,
so adding a track never turns the others down. `master.wav` is 32-bit float, so a hot sum
cannot clip in the file; the peak is reported after every mix, and gain per track is there to
bring it down before an mp3 bounce.

A soft trim is applied after decoding, so it is sample-exact for wav and mp3 alike, and the
sound stays where it is on the timeline when you change the in point: the offset is where
the file starts, the in point just reveals less of it.

A hard trim keeps the sound where it is too. For wav it is exact. For mp3 the frames are
copied without re-encoding (a 26 ms grid), and the file's LAME encoder delay is accounted
for so the track moves by less than a millisecond; `-r` re-encodes for an exact cut.
Hard trims and `rm -D` are the only things `undo` cannot take back.

## `gout cut`

```
gout cut INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE] [-r] [-f] [-n] [-v]
gout INPUT ...     the 1.x form, same thing
```

| flag | meaning |
| --- | --- |
| `-st`, `--start` | start time, from the beginning of the file (default `0`) |
| `-et`, `--end` | absolute end time |
| `-el`, `--length` | length of the cut |
| `-fs`, `--file-size` | cut as much as fits in this file size |
| `-o`, `--output` | output path (default `<name>_cut.mp3`) |
| `-r`, `--reencode` | re-encode instead of copy: exact, slower |
| `-f`, `--force` | overwrite the output |
| `-n`, `--dry-run` | show what would be cut, write nothing |
| `-v`, `--verbose` | show the ffmpeg commands and each size-fitting pass |

`-fs` estimates the length from the bitrate, cuts, measures and retries until the file lands
between 97 % and 100 % of the limit, never over it. An mp3 stream copy starts up to about
0.1 s late (that is ffmpeg's seek); `-r` is exact. wav cuts are exact.

## Times and sizes

```
00:34:00        HH:MM:SS            34:00     MM:SS
00:34:00.500    HH:MM:SS.mmm        34        a bare number is MINUTES
00:34:00:500    HH:MM:SS:mmm        90s  2.5m  1.5h  500ms   explicit units

1.99            a bare number is MB           700MB  1.99GB  500kB   decimal
25MiB  1.99GiB  binary units
```

Use the explicit units for nudges: `move 2 +500ms`, not `move 2 +0.5`.
