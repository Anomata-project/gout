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

`add` takes any path and copies the file in; the original is never touched. Files you copy or
move into `master/` yourself are picked up by `gout scan`, which registers the new ones at 0 and
leaves the existing tracks alone. Renaming a file inside `master/` by hand breaks the track that
points at it: `scan` reports it as missing. `gout rebuild` is the last resort: it recreates
`gout.db` from whatever is in `master/`, every track at 0 with nothing else remembered.

## Commands

Every command has a long and a short name; `gout add` and `gout a` are the same. Run them
inside the project, or pass `-p DIR` first. `TRACK` is the number shown by `ls` or the track
name (a unique prefix will do). `-N` / `--no-mix` on any change skips the automatic re-mix;
`gout set autorender off` turns it off for good. `gout cheat` prints the whole sheet.

| long | short | arguments | meaning |
| --- | --- | --- | --- |
| `new` | `n` | `NAME [-R HZ]` | create a project (48 kHz by default) |
| `add` | `a` | `FILE... [-a TIME] [-n NAME]` | add tracks at `TIME` (default 0) |
| `scan` | `sc` | | register wav/mp3 files you copied into `master/` yourself; reports missing ones |
| `ls` | `l` | | list tracks, positions, trims, flags |
| `view` | `v` | `[-w COLS]` | print the timeline once, envelopes included (`░` marks soft-trimmed material there) |
| `move` | `m` | `TRACK +TIME \| -TIME \| TIME` | nudge later, nudge earlier, place at a time |
| `trim` | `t` | `TRACK [-st T] [-et T \| -el T]` | soft trim, times count from the start of the track's file |
| `trim` | `t` | `TRACK -c` | soft trim off (`--clear`) |
| `trim` | `t` | `TRACK -H [-st ..] [-et ..] [-r]` | hard trim, rewrites the file; bakes the soft trim when no times given (`--hard`) |
| `rm` | `r` | `TRACK [-D]` | drop a track; `-D` also deletes its file (`--delete`) |
| `mute` | `mu` | `TRACK [on\|off]` | toggle; `mute all off` |
| `solo` | `s` | `TRACK [on\|off]` | toggle; `solo all off` |
| `gain` | `g` | `TRACK DB` | `gain 2 -6` |
| `pan` | `p` | `TRACK L30 \| R30 \| C` | every track starts centred |
| `mix` | `x` | `[-3] [-v]` | render `master.wav`; `-3` / `--mp3` also writes `master.mp3` |
| `undo` | `u` | | undo the last change (not a hard trim or `rm -D`) |
| `stems` | `sm` | `[DIR] [-A]` | one wav per track, processed as in the mix and all the same length, into `stems/`; `-A` only what the mix hears |
| `dump` | `dp` | | the state as JSON |
| `rebuild` | `rb` | `[-f]` | recreate `gout.db` from `master/` |
| `set` | `se` | `KEY VALUE` | settings; `set` alone lists them all (see the master bus below) |
| `stats` | `st` | | integrated LUFS, loudness range and true peak per track file, and for `master.wav` |
| `cheat` | `c` | | the cheat sheet |
| `cut` | | `INPUT ...` | the 1.x cutter, see below |

Long flags exist for every short one: `--at --name --hard --clear --reencode --delete --mp3
--rate --width --verbose --no-mix`.

## The terminal ui

`gout` inside a project (or `gout ui`) opens a split screen. The left side is a prompt that
takes the same commands without the leading `gout`. It behaves like a terminal: the prompt sits
right under the last output line and walks down the screen, then stays on the bottom row while
the log scrolls (`pgup` / `pgdn` look back, typing snaps back down). The right side is the timeline, one row per track, drawn as a waveform envelope: each column is a block
`▁▂▃▄▅▆▇█` as tall as the loudest peak in that slice of time, six dB per step, so a full block
is louder than -6 dBFS and the flat bottom line is silence. Soft-trimmed material is dimmed,
a muted or un-soloed track is dimmed all over, and `master.wav` gets a row of its own so you
see the sum. Below the tracks sits the cheat
sheet; `tab` and `shift-tab` flip its pages, `ctrl-n` / `ctrl-p` move it a line. It is a
picture, not a mouse target: the keyboard drives everything.

```
 gout song  48000 Hz  4 tracks  autorender on │ timeline
 > add vocals.mp3                          │                0:00        0:30        1:00
 add    4  vocals  mp3  2ch ...            │                ┼───────────┼───────────┼──────
 mix   master.wav  00:02:01.000  peak -3.1 │  1 drums       ▆█▆█▆█▆█▆█▆█▆█▆█▆█▆█▆█▆█▆█▆█
 > move 4 +1.5s                            │  2 bass        ▅▅▆▅▅▆▅▅▆▅▅▆▅▅▆▅▅▆▅▅▆▅▅▆▅▅▆▅
 move   4  vocals  at 00:00:01.500 -> ...  │  3 gtr      M     ▁▂▄▅▆▅▄▆▇▆▅▄▃▂▁
 mix   master.wav  00:02:01.000  peak -3.1 │  4 vocals              ▃▅▇▆▅▇█▇▅▆▇▆▄▅▇▆▅▃▂▁
 > _                                       │    master.wav  ▇█▇█▇█▇█▇█▇█▇█▇█▇█▇█▇█▇█▇█▇█
                                           │ cheat sheet  1/3  tab
                                           │ TRACKS
                                           │  add   a  FILE.. [-a TIME] [-n NAME]  copy into master/
                                           │  move  m  TRACK +1s | -500ms | 1:30   later|earlier|place
```

The two right-hand sections are independent: `ctrl-u` or `view` hides and shows the timeline,
`ctrl-k` or `cheat` the cheat sheet (`tab` brings it back too). Hide both and the prompt gets the
whole width. `help` prints the sheet into the log, `help all` the whole instruction page,
`quit` / `ctrl-d` / `ctrl-c` leave. Both states are remembered per project.

### The parameter sheet

`ctrl-e` (or `sheet` at the prompt) replaces the screen with a table of every parameter in the
database: the project and master settings first, then each track with its `at`, `in`, `out`,
`gain`, `pan`, `mute` and `solo`. Three columns: name, value, new value. Move with the arrows,
type into the third column, `ctrl-w` clears a cell. `ctrl-s` applies every edited row as the
ordinary command it stands for, so each change is undoable, then renders once. `ctrl-x` applies
and closes, `esc` closes and keeps unapplied edits for next time. A rejected value stays in the
sheet marked `!` with the reason on the bottom line. The ui switches the terminal's flow control
off for its own session so that `ctrl-s` reaches it.

`saveas NAME` (`sa`, also `gout saveas` from the shell) copies the whole project, audio
included, to a sibling directory with that name, or to a path when you give one, and the ui
carries on in the copy the way a DAW's Save As does. In the sheet, `ctrl-shift-s` asks for the
name in terminals that can send that key distinctly (kitty, foot, wezterm and friends).

`ctrl-←` / `ctrl-→` move the split between the panes (shift or alt with the arrows work too),
or type `split 50`, `split +5`, `split -5`. The width is remembered per project. There is no
mouse dragging on purpose: turning on mouse reporting would stop ordinary text selection in
the terminal.

## The master bus

Everything on the master is a setting: `gout set KEY VALUE`, `gout set` alone lists them, and
every change re-renders `master.wav` (unless autorender is off) and reports the result:

```
mix   master.wav  00:03:12.500  -14.0 LUFS  LRA 6.2  peak -1.0 dBTP
      linear gain +3.4 dB
```

| setting | meaning |
| --- | --- |
| `lufs -14` / `off` | loudness target. Two passes of ffmpeg's `loudnorm`: a plain gain change whenever the ceiling allows, otherwise dynamic, and the mix line says which. -14 for Spotify and YouTube, -16 for Apple Music and podcasts, -23 for EBU broadcast. Default off |
| `ceiling -1` | true-peak ceiling in dBTP for the loudness step |
| `gain -3` | master gain in dB, before the loudness step |
| `fadein 500ms`, `fadeout 3s` | fades on the sum |
| `head 500ms`, `tail 2s` | silence padded before and after |
| `bits 32f` / `24` / `16` | `master.wav` format; 16 is dithered |
| `mp3 320k` / `192k` / `v0` | quality of the `mix --mp3` bounce |
| `title`, `artist`, `album`, `year`, `comment` | tags written into `master.wav` and `master.mp3` |

The chain is: sum of the tracks, master gain, fades, loudness step, head and tail padding, then
the file. Under three seconds of material the loudness step is a plain gain, since `loudnorm`
cannot measure that reliably. `gout stats` shows integrated LUFS, loudness range and true peak
for every track file (and what it comes to after the track's gain), so you can balance tracks
by numbers before touching the master.

## Stems

`gout stems` writes one stereo wav per track into `stems/`, each trimmed, placed on the
timeline, gained and panned exactly as the mix hears it, and padded so every file has the same
length from 0:00. Drop them into any DAW at zero and you have the mix. Mute and solo are ignored
so every track comes out; `-A` exports only what the mix currently hears. Master gain, fades,
padding and the loudness target are not applied to stems; without those, the stems summed at
unity are `master.wav` exactly.

## How the mix works

Every track is decoded, trimmed to its in/out points, delayed to its position, panned to
stereo (mono goes equally to L and R) and summed with ffmpeg's `amix` with normalisation off,
so adding a track never turns the others down. `master.wav` is 32-bit float by default, so a
hot sum cannot clip in the file; loudness and true peak are reported after every mix, and a
loudness target or the gains bring it down before an mp3 bounce.

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
