# gout

A command-line DAW. Stack `wav` and `mp3` tracks on a timeline, trim and move them, and mix
them to a stereo `master.wav`. The original cutter is still in there as `gout cut`.

Wraps `ffmpeg`. Everything else is the Python standard library: no dependencies, no virtualenv.
The code is the `gout/` package; `bin/gout` runs it straight from the checkout.

## Install

Installers for Windows, macOS and Linux (Ubuntu, Debian) are on the
[releases page](https://github.com/Anomata-project/gout/releases/latest): open one, click through,
and open **gout** from the Start menu, Launchpad or your applications. The Windows and macOS
installers carry their own Python and ffmpeg; the Linux package has apt bring them. gout is not
signed with a paid certificate, so Windows and macOS ask once whether you trust it; the release
notes show the two clicks that answer.

### From source

Needs Python 3.9 or newer and `ffmpeg` and `ffprobe` on `PATH`. On Windows the terminal ui also
needs `pip install windows-curses`.

```sh
./install.sh              # symlinks ~/.local/bin/gout -> ./bin/gout
```

The symlink points back at this checkout, so editing the package takes effect immediately.
`pipx install .` and `pip install --user -e .` work too, or run `./bin/gout` or `python3 -m gout`
in place. `packaging/build.py` builds the installers; `.github/workflows/installers.yml` builds and
tries them on every push and publishes them for a tag.

## A project

```sh
gout new song && cd song
gout add drums.mp3 bass.wav          # copied into master/, master.wav mixed
gout add vocals.flac --at 00:00:08   # other formats are decoded to wav on the way in
gout move 3 +250ms                   # nudge later; -250ms earlier; 1:30 places it there
gout trim 3 -st 2s -et 1:40          # soft trim: in/out points, the file is untouched
gout trim 3 -et -5s                  # the last 5 seconds left out
gout trim 3 --hard                   # hard trim: bake it into the file, lighter material
gout solo 3 && gout mix --mp3        # hear one track, bounce master.mp3 as well
gout undo                            # every change is recorded
gout                                 # open the terminal ui
```

A project is a directory:

```
song/
  gout.db        sqlite: track order, positions, trims, gain, pan, mute/solo, undo history, caches
  gout.json      the same state as a readable document, rewritten after every change
  master/        the track files, wav or mp3, never modified by moves or soft trims
  master.wav     the mix, 32-bit float stereo at the project rate (48 kHz by default)
```

`gout.json` is the readable twin of the database: the same document `gout dump` prints, rewritten
atomically after every change. It holds the settings and every track's file, position, trims,
gain, pan, mute and solo, in plain units (milliseconds, dB, pan from -1 to 1). It is there to
read, to diff in git, and to recover from: `gout rebuild` uses it to put everything back where it
was. `gout import FILE.json` applies such a document to the current project; `-s` takes only the
settings, which turns a project into a master template, `-t` only the tracks, matched by file name.

`add` takes any path and copies the file in; the original is never touched. Files you copy or
move into `master/` yourself are picked up by `gout scan`, which registers the new ones at 0 and
leaves the existing tracks alone. Renaming a file inside `master/` by hand breaks the track that
points at it: `scan` reports it as missing. `gout rebuild` is the last resort: it recreates
`gout.db` from whatever is in `master/`, then restores positions, trims and settings from
`gout.json` when it is there, and otherwise leaves every track at 0.

## Commands

Every command has a long and a short name; `gout add` and `gout a` are the same. Run them
inside the project, or pass `-p DIR` first. `TRACK` is the number shown by `ls` or the track
name (a unique prefix will do). `-N` / `--no-mix` on any change skips an automatic render when autorender is on (see Playing). `gout cheat` prints the whole sheet.

| long | short | arguments | meaning |
| --- | --- | --- | --- |
| `new` | `n` | `NAME [-R HZ]` | create a project (48 kHz by default) |
| `add` | `a` | `FILE... [-a TIME] [-n NAME]` | add tracks at `TIME` (default 0) |
| `scan` | `sc` | | register wav/mp3 files you copied into `master/` yourself; reports missing ones |
| `ls` | `l` | | list tracks, positions, trims, flags |
| `view` | `v` | `[-w COLS]` | print the timeline once: master first, tracks as waveforms |
| `colors` | | `[--init [--project] [-f]]` | the colour and layout settings of `color.json` |
| `move` | `m` | `TRACK... \| all +TIME \| -TIME \| TIME` | nudge later, nudge earlier, place at a time; `move 1 3 -12s` or `move all -12s` moves several by the same amount (one undo), and with `TIME` the earliest of them lands there with the spacing kept |
| `trim` | `t` | `TRACK [-st T] [-et T \| -el T]` | soft trim, times count from the start of the track's file; with a minus from its end: `trim 2 -et -5s` leaves out the last 5 seconds |
| `trim` | `t` | `TRACK -c` | soft trim off (`--clear`) |
| `trim` | `t` | `TRACK -H [-st ..] [-et ..] [-r]` | hard trim, rewrites the file; bakes the soft trim when no times given (`--hard`) |
| `rm` | `r` | `TRACK [-D]` | drop a track; `-D` also deletes its file (`--delete`) |
| `mute` | `mu` | `TRACK [on\|off]` | toggle; `mute all off` |
| `solo` | `s` | `TRACK [on\|off]` | toggle; `solo all off` |
| `gain` | `g` | `TRACK DB` | `gain 2 -6` |
| `pan` | `p` | `TRACK L30 \| R30 \| C` | every track starts centred |
| `hp`, `lp` | | `TRACK HZ [SLOPE] \| off` | high-pass or low-pass cut, slope in dB per octave (12 by default) |
| `eq` | `e` | `TRACK BANDS... \| PRESET \| on \| off \| clear` | the whole eq in one line; `eq TRACK` shows it with the curve |
| `delay` | `dl` | `TRACK TIME [wN fN nN] \| PRESET \| on \| off \| clear` | delay after the compressor; note values with `set bpm` |
| `reverb` | `rv` | `TRACK DECAY [pN dN wN] \| PRESET \| on \| off \| clear` | reverb after the fader; `reverb TRACK` draws its decay |
| `comp` | `cp` | `TRACK SETTINGS... \| PRESET \| on \| off \| clear` | compressor after the eq; `comp TRACK` shows its curve |
| `fx` | `f` | `TRACK [add KIND ... \| N SETTINGS \| N on\|off\|rm \| N move M \| clear]` | the track's (or master's) effect chain, in order; `fx kinds` lists every effect |
| `play` | `pl` | `[FROM] [-r]` | play `master.wav`, or the project live when it is out of date; in the ui, space plays and stops |
| `mix` | `x` | `[-3] [-v]` | render `master.wav`; `-3` / `--mp3` also writes `master.mp3` |
| `record` | `rec` | `[FROM] [-t LENGTH] [-n NAME] [-i INPUT] [-c N] [-s] [-d]` | record a new track while the project plays from `FROM`; see Recording |
| `record` | `rec` | `calibrate [-i INPUT] [-c N]` | play clicks and record them, so later takes land on time |
| `inputs` | `in` | `[N \| NAME \| default]` | what can be recorded; `N` picks one for this computer |
| `undo` | `u` | | undo the last change, again for the one before; `ctrl-u` in the ui (not past a hard trim or `rm -D`) |
| `stems` | `sm` | `[DIR] [-A]` | one wav per track, processed as in the mix and all the same length, into `stems/`; `-A` only what the mix hears |
| `dump` | `dp` | | the state as JSON, the same document as `gout.json` |
| `import` | `im` | `FILE.json [-s \| -t]` | apply a document: settings and tracks, or only one of them |
| `rebuild` | `rb` | `[-f]` | recreate `gout.db` from `master/`, restoring state from `gout.json` when present |
| `set` | `se` | `KEY VALUE` | settings; `set` alone lists them all (see the master bus below) |
| `stats` | `st` | | integrated LUFS, loudness range and true peak per track file, and for `master.wav` |
| `cheat` | `c` | | the cheat sheet |
| `help` | `h` | `[all \| COMMAND]` | the instruction page; `help record` shows one command's part of it |
| `cut` | | `INPUT ...` | the 1.x cutter, see below |

Long flags exist for every short one: `--at --name --hard --clear --reencode --delete --mp3
--rate --width --verbose --no-mix`.

## Playing

```sh
gout play            # from the start; ctrl-c stops and says where
gout play 1:30       # from a minute and a half in
gout play -r         # render master.wav first, then play the file
```

When `master.wav` matches the project, `play` plays it. When it does not (you changed something
since the last render), `play` streams the project live: the tracks, their effects, the master
chain, gain and fades run straight into the player, starting at once from the playhead. Audio
before the playhead is cut off before any effect sees it, so starting late in a long song costs
nothing. A loudness target cannot be applied live (it needs the whole mix), so live playback uses
the gain the last render measured, with a limiter holding peaks under -1 dBFS.

It plays through `ffplay` when ffmpeg came with it, otherwise it pipes decoded audio into `pw-cat`
(PipeWire), `paplay` (PulseAudio) or `aplay` (ALSA). `GOUT_PLAYER=paplay` picks one;
`GOUT_PLAYER=null` plays in real time without sound; `GOUT_PLAYER=file:out.wav` writes what would be
heard to a file.

In the ui, space on an empty prompt plays and stops, like the space bar in a DAW. Stopping leaves
the playhead where it was and the next play carries on from there; `stop` again, or playing to the
end, puts it back at the start. While the prompt is empty, left and right move the playhead five
seconds. The timeline header shows the position (and `live` when streaming) and a marker runs across
the tracks.

### When master.wav is rendered

`gout set autorender idle|on|off` decides. `idle`, the default, makes changes instant: nothing is
rendered when you add an effect or move a track. In the ui, once nothing has changed for about a
second and a half, `master.wav` is rendered in a separate process while you keep working, and a
new change cancels it and waits again. The timeline draws an out-of-date master in grey and says
so. `on` renders after every change, from the shell too, as older versions did; `off` renders only
when you run `mix`. `stems`, `mix` and `play -r` always render what they need.

## Recording

Recording runs from a shell in the project folder; the ui cannot do it yet.

```sh
gout inputs                   # what can be recorded here; the one in use is marked
gout inputs 2                 # use input 2 from now on, on this computer (default: the system's again)
gout record calibrate         # once per input and output: 10 clicks out and back in
gout record                   # a new track from 0:00 while the project plays; ctrl-c stops
gout record 1:30 -n vocal     # from 1:30, the track called vocal
gout record 0:12 -t 30s       # stops by itself after 30 seconds
```

Each take is a new track: a 32-bit float wav in `master/`, called `rec` unless `-n` names it. It
records mono from channel 1 of the input; `-c 2` takes channel 2 and `-s` stereo from channels 1
and 2 (or N and N+1). `-i` records from another input this once. `undo` deletes the take. A take
cut short by a crash is still a file in `master/`, and `gout scan` registers it.

While you record, the project plays from `FROM`, and the take is placed so it lines up with what
you heard: gout measures how late the input is on every take and trims that off. `gout record
calibrate` makes it exact, to within half a millisecond. Put the microphone near the speaker, or a
cable from the output to the input, and it plays 10 clicks and records them. Do it once for each
input and output (headphones and speakers count as different outputs). The result is kept for this
computer, and every later take uses it.

gout plays the project but does not play your input back to you, so wear headphones: with
speakers the microphone records the song too. `-d` records without playing anything. gout also
records without playing when PortAudio is missing (`gout version` says so) or when nothing is
audible from `FROM`.

On Linux, `gout inputs` also lists what each output plays, so you can record the computer's own
sound like any other input. Capture goes through `pw-record`, `parecord` or `arecord` on Linux and
ffmpeg on macOS and Windows. `GOUT_RECORDER` picks one, and `GOUT_RECORDER=null` records silence.

## The terminal ui

### The timeline

The master comes first, then every track, each drawn as a waveform from the audio's real highest
and lowest points in braille dots (two columns and four rows of dots per character), with a gap
row between them. The label shows the track's number, name, mute and solo, and underneath its gain
and pan; the master's shows its loudness. Soft-trimmed material is drawn dim, muted tracks in
grey, silence as a thin centre line. When the screen is short, tracks shrink to one row, then the
master, then the gaps go, and what still does not fit is counted.

### color.json

Colours and the timeline's layout come from `color.json`: the project's own if it has one,
otherwise `~/.config/gout/color.json`, otherwise the built-in defaults. `gout colors` shows which
file is in use, anything wrong in it, and all 25 settings with their values; `gout colors --init`
writes the defaults (each explained under `_help`) to fill in, `--project` into the project.

| setting | default | what |
| --- | --- | --- |
| `master_height`, `track_height` | `2`, `2` | rows for the waveforms, 1 to 6 |
| `gap_rows`, `gap_char` | `1`, `"┈"` | rows between tracks and what they are drawn with |
| `wave_style` | `"braille"` | or `"blocks"` for fonts without braille |
| `wave_scale` | `"linear"` | or `"db"`, which makes quiet passages visible |
| `master_wave`, `master_label` | amber | the master |
| `track_palette` | six colours | tracks take them in turn |
| `track_label`, `muted_wave`, `trimmed_wave`, `center_line` | greys | labels, muted, trimmed, silence |
| `ruler`, `ruler_labels`, `playhead`, `gap_line` | | the rest of the timeline |
| `header`, `prompt`, `command_echo`, `error`, `suggestion` | | title bars and the prompt |
| `cheat_heading`, `effect_curve`, `sheet_edit` | | cheat sheet, effect pictures, sheet edits |

A colour is `"#rrggbb"`, `"#rgb"`, an xterm number, a name (`red`, `bright_cyan`, `default` ...), or
`{"fg": ..., "bg": ..., "bold": true, "dim": true, "underline": true, "reverse": true}`. gout picks
the nearest colour the terminal has; without colour everything falls back to bold, dim and reverse.

`gout` inside a project (or `gout ui`) opens a split screen. The left side is a prompt that
takes the same commands without the leading `gout`. It behaves like a terminal: the prompt sits
right under the last output line and walks down the screen, then stays on the bottom row while
the log scrolls (`pgup` / `pgdn` look back, typing snaps back down). The right side is the timeline, one row per track, drawn as a waveform envelope: each column is a block
`▁▂▃▄▅▆▇█` as tall as the loudest peak in that slice of time, six dB per step, so a full block
is louder than -6 dBFS and the flat bottom line is silence. Soft-trimmed material is dimmed,
a muted or un-soloed track is dimmed all over, and `master.wav` gets a row of its own so you
see the sum. Below the tracks sits the cheat
sheet; `tab` and `shift-tab` flip its pages, `ctrl-n` / `ctrl-p` move it a line. It lays itself
out for the panel's width: narrow, descriptions wrap under their commands; wider, arguments and
descriptions share a line and every effect's full preset list shows; from about 130 columns it
flows into two columns. Move the split with `ctrl-←` / `ctrl-→` and it reflows. `gout cheat -w 150`
prints it at a given width. It is a picture, not a mouse target: the keyboard drives everything.

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
                                           │  add   a  FILE... [-a TIME] [-n NAME]
                                           │              copy files into master/
                                           │  move  m  TRACK +1s | -500ms | 1:30
                                           │              later, earlier, or place at a time
```

The two right-hand sections are independent: The prompt edits like a shell. Left and right arrows, Home and End (or ctrl-a) move the cursor
and typing goes in where it is; up and down bring back earlier commands, kept per project in
`.gout/ui-history`, ready to change a letter and run again. Tab completes the word under the
cursor: a command, a track name, `master`, an effect preset, or a file or folder relative to where
gout was started. Names with spaces come out escaped (`Sandi\ piano\ .m4a`), so they reach the
command as one word. While you type, the rest of a matching earlier command (or the only possible
completion) shows in grey after the cursor; the right arrow at the end of the line takes it. On an
empty line Tab still flips the cheat sheet. `gout add` also finds a file whose name has spaces when
it was typed without quotes.

`ctrl-u` undoes the last change, the same as typing `undo`, and pressing it again undoes the one
before. It works whatever is on the line and leaves the line alone. `ctrl-t` or `view` hides and
shows the timeline,
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

The line above the bottom one shows the highlighted row as a whole command, never cut short:
`gout eq 1 hp35 +6@65/1.2 -3@300/1.5 +3@2k`, `gout gain 1 -3`, `gout set title 'Deep water'`
(a second effect of the same kind goes by its position, `gout fx 1 3 ...`). Select it with the
mouse and paste it into a shell or into gout's prompt in another project: the prompt takes a
leading `gout` too. `ctrl-p` closes the sheet with that command on the prompt line, ready to
change the track number and run. A value too long for its column ends in `…`.

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
| `eq hp30 hs10k:+1`, `comp glue` | eq and compressor on the master, same syntax and presets as a track's; `eq master ...` and `comp master ...` do the same and draw the curves from `master.wav` |
| `gain -3` | master gain in dB, before the loudness step |
| `fadein 500ms`, `fadeout 3s` | fades on the sum |
| `head 500ms`, `tail 2s` | silence padded before and after |
| `bits 32f` / `24` / `16` | `master.wav` format; 16 is dithered |
| `mp3 320k` / `192k` / `v0` | quality of the `mix --mp3` bounce |
| `title`, `artist`, `album`, `year`, `comment` | tags written into `master.wav` and `master.mp3` |

The chain is: sum of the tracks, the master's effect chain, master gain, fades, loudness step,
head and tail padding, then the file. Per track, before the sum: soft trim, position, the
track's effect chain in order, gain, pan. Under three seconds of material the loudness step is a plain gain, since `loudnorm`
cannot measure that reliably. `gout stats` shows integrated LUFS, loudness range and true peak
for every track file (and what it comes to after the track's gain), so you can balance tracks
by numbers before touching the master.

## Effects

Every track has an effect chain, and so does the master. The audio goes through the effects in
order, then through the track's gain and pan, like the inserts and the fader of a mixer strip.

```
gout eq 3 hp80                 # the first eq on track 3; added where an eq usually goes if missing
gout comp 3 vocal              # likewise: eq, comp, delay, reverb find their usual places
gout fx 3                      # the chain, numbered
       1  eq hp80
       2  comp -20 3:1 a5 r120 k4 m4
gout fx 3 add eq +2@5k         # a second eq, at the end
gout fx 3 3 move 1             # reorder: slot 3 to the front
gout fx 3 2 off                # bypass slot 2;  on, rm, or new settings work the same way
gout fx 3 clear                # remove them all
gout fx master add reverb room # the master chain, before master gain and fades
gout fx kinds                  # every effect there is, built in or from an addon
```

`gout KIND TRACK ...` (`eq`, `comp`, `delay`, `reverb`, and any addon's) always works on the
first effect of that kind in the chain; `fx` reaches any slot. In the parameter sheet each effect
is a row named by its slot and kind: type new settings, `off`, `on`, `rm` or `move 1` into it, and
the `+ effect` row takes `KIND SETTINGS` to add one. `gout.json` stores each chain as a list:

```json
"fx": [{"kind": "eq", "params": "hp80", "on": true}, {"kind": "comp", "params": "-20 3:1 a5 r120 k4 m4", "on": true}]
```

Projects from before chains, and their `gout.json` files, are converted when opened or imported.
An effect a project uses but this machine does not have (an addon not installed) stays in the
chain, is marked `(not installed)`, and is left out of the mix with a warning on every render.

## Addons

An addon is a Python file that adds effects or full-screen views. Put it in `~/.config/gout/addons/`
(`%APPDATA%\gout\addons\` on Windows; `$XDG_CONFIG_HOME/gout/addons/` or the folder `$GOUT_ADDONS`
names when set) and its effects behave like the built-in ones: a command with presets and a
picture, a place in `gout fx` chains, rows in the parameter sheet, entries in `gout.json`, help and
the cheat sheet.

```sh
gout addons examples             # copy the examples below into the addon folder
gout addons                      # the folder, what loaded, and effects this project lacks
```

[docs/addons.md](docs/addons.md) shows how to write your own, from a first effect to screens.

Five examples ship in `examples/addons/`:

| addon | command | what it does |
| --- | --- | --- |
| `chorus.py` | `chorus` / `ch` | stereo chorus: `v3 r0.5 d2 t25 m50 w100` is voices per side, rate Hz, depth ms, delay ms, mix %, width %. Left and right get different voices, so a mono track comes out wide. Presets `subtle classic wide vibe`. Its picture shows how each voice's delay swings. |
| `saturation.py` | `saturation` / `sat` | a soft curve for warmth, grit or fuzz: `tanh d12 m70 t8k` is curve, drive dB, mix % (parallel blend), tone low-pass. Curves `tanh atan cubic exp alg quintic sin erf hard`. Output follows the drive by default so quiet passages keep their level; `o-6` sets it by hand. It runs at four times the project rate to keep aliasing down. Presets `warm tape tube crunch fuzz`. Its picture is the curve itself and says how many of the track's peaks it bends. |
| `distortion.py` | `distortion` / `dist` | a pedal in a line: `hard d36 a10 h300 t4k` is clipper (`soft`, `hard` or `crush`), drive dB, asymmetry % (even harmonics), tight high-pass before the clipper, tone low-pass after. `crush` takes `b6` bits and `s8` sample-rate reduction. The output is matched to the track's loudness by running part of the track through the same stage once (cached); `o-6` sets it by hand. Presets `overdrive crunch highgain fuzz bitcrush lofi broken`. |
| `tremolo.py` | `tremolo` / `trem` | the volume rises and falls: `5hz d50`. Presets `slow fast chop`. |
| `fractal.py` | `ctrl-space`, or `fractal` / `fz` in the ui; `zoom` / `zm` | full-screen play: the song as a Newton fractal, moving with the music. Ten presets and formulas of your own. `zoom` dives into the same fractal for as long as the song lasts. See below. |

`examples/addons/tremolo.py` is the simplest template: a subclass of `gout.fx.Effect` that says how to read
and write its settings line and which ffmpeg filters it becomes, and a `register(gout)` function
that calls `gout.add_effect(...)`. Effects that need more than a filter list (the built-in reverb
convolves with a generated file; the chorus treats left and right apart; the saturation blends a
clean path back in) override `graph` instead of `filters`. The base class in
`gout/fx.py` documents every hook.

### Screens

A screen is a full-screen view an addon adds to the ui. `examples/addons/fractal.py` is one:
`ctrl-space` fills the terminal with it and plays from the playhead (in GNOME Terminal the window
goes fullscreen too), `esc` goes back to gout with the song still playing. Space plays and stops,
the left and right arrows move 5 s, `1` .. `9` and `0` pick one of ten presets and up and down step
through them, `+` and `-` zoom, `c` turns colours off. At the prompt, `fractal rings` or `fractal 5`
opens a preset, `fractal z^5 - 3z + 1` a formula of your own, and `fractal presets` lists them; the
presets live in `~/.config/gout/fractal.json`, written the first time.

Every character is a starting point z on the complex plane that Newton's method walks towards a
root of the formula: the character says how many steps it took, the colour which root it reached,
and where the basins meet the steps pile up into the fractal. The music moves it. The bass bends
the method and pushes the rotation, the overall level zooms in, hits and highs make it denser, and
the mids trade the basins' colours. gout listens to the song once (about 1.5 s for four minutes,
cached in `.gout/analysis/`): from `master.wav` when it is up to date, otherwise from the track
files as the timeline places them, without their effects.

`zoom` (or `zm`) is the same fractal diving in for as long as the song plays, and
`gout video zoom 3 1 0` puts it into a video instead of the fractal. It heads for a point Newton's
method keeps coming back to every two or three steps; around such a point the picture repeats a
few times smaller each time, so once the view is 100000 times deeper it goes back up one repeat
without a visible jump and carries on. The status line counts the zoom all the same. Time pushes
it, the level and the bass push it harder, in silence it only drifts; the bass does not bend the
method here. The same keys and presets as the fractal: a preset key or up and down dives into
another preset, `+` and `-` go nearer and further, and `zoom_preset` in `fractal.json` remembers
its choice.

A screen is a subclass of `gout.screens.Screen` with a `frame(ctx, width, height)` that returns
rows of text and colour classes, registered with `gout.add_screen(...)`. `ctx.band("low")` gives
how loud a band is now (`low`, `mid`, `high`, `level`, `onset`, 0 to 1) and `ctx.travel("low")`
how much of it has gone by, for motion that pushes with the music. A screen takes one of the keys
nothing else uses: `ctrl-space`, `ctrl-b`, `ctrl-o`, `ctrl-q`, `ctrl-r`, `ctrl-v`, `ctrl-y`.

An addon that fails to load, or wants a name that is taken, is reported on every command and
skipped; gout keeps working. Addons are ordinary Python running with your permissions, so gout
reads them only from your own folder, never from a project folder.

## EQ

Every track has an eq, applied before its fader, written as one line of bands:

```
gout hp 3 80                    # high-pass at 80 Hz, 12 dB per octave
gout hp 3 80 24                 # steeper
gout lp 3 12k                   # low-pass
gout eq 3 hp80 +3@200 -4@2.5k/3 hs8k:-2     # the whole eq at once
gout eq 3 voice                 # a preset, or   eq 3 voice +1@5k   to build on one
gout eq 3 off | on | clear      # bypass, bring back, remove
gout eq 3                       # show the bands and draw the curve
gout eq presets                 # the preset list
```

Bands: `hp80` and `lp12k` are cuts, with `/24` for the slope in dB per octave (6 to 48). `+3@200`
is a peak of +3 dB at 200 Hz; `/3` after it sets the Q (1 by default). `ls100:+2` and `hs8k:-3`
are low and high shelves. Frequencies take `k`, gains are dB. The mix and the stems use ffmpeg's
highpass, lowpass, equalizer, lowshelf and highshelf filters.

`gout eq 3` draws the frequency response from 20 Hz to 20 kHz on a log axis, with the track's own
average spectrum dimmed behind it, so you see what you are cutting. In the ui the same picture
sits in the right panel and follows whichever track you last touched with `eq`, `hp` or `lp`;
`ctrl-g` or `eq` alone hides and shows the pictures. The name line with the effect and its
settings stays, and keeps following your changes; the choice is remembered per project.

Presets: `voice`, `podcast`, `warm`, `air`, `bright`, `mud`, `clean`, `phone`, `bass`, `kick`,
`guitar`, `flat`. A preset expands to ordinary bands, so what you see in `ls` and the sheet is
always the real eq.

## Compressor

Every track has a compressor after its eq, written as one line:

```
gout comp 3 -18 4:1                      # threshold -18 dB, ratio 4:1, the rest at defaults
gout comp 3 -20 3:1 a5 r120 k4 m4        # attack ms, release ms, knee dB, makeup dB
gout comp 3 -24 8:1 mauto                # makeup picked for you
gout comp 3 vocal                        # a preset, or   comp 3 drums a2   to build on one
gout comp 3 off | on | clear
gout comp 3                              # show it with its curve
gout comp presets
```

`gout comp 3` draws the static curve, input level across and output level up, both from -60 to
0 dB, with the unity line dotted. Behind it sits a histogram of where this track's own peaks
fall, so you see at once whether the threshold is in the material or above it, and the header
says how often the compressor would work on this track and by how much. Attack and release are
not in the picture; they are ffmpeg's `acompressor` values. Presets: `gentle`, `vocal`, `drums`,
`bass`, `glue`, `squash`, `limit`, `none`.

In the ui the compressor curve sits next to the eq curve in the same panel when it is wide
enough, and otherwise the panel shows whichever of the two you touched last.

## Delay

Every track has a delay after its compressor, and the master has one after its compressor too:

```
gout delay 3 375ms w30 f40 n4        # time, wet %, feedback %, repeats
gout set bpm 120                     # then times can be note values
gout delay 3 1/8                     # 1/4 1/8 1/16 3/16, 1/8d dotted, 1/8t triplet
gout delay 3 slap | eighth | quarter | dotted | long
gout delay 3 off | on | clear
gout delay 3                         # the repeats drawn over time, in dB
gout delay master 1/4 w15            # or: gout set delay 1/4 w15
```

Each repeat is feedback % quieter than the one before, the first at the wet level. The tempo is
a project setting, so changing `bpm` moves every note-value delay with it.

## Reverb

Every track has a reverb after its delay, and the master has one too:

```
gout reverb 3 2.5s p20 d50 w25       # decay to -60 dB, pre-delay ms, damping %, wet %
gout reverb 3 hall                   # ambience room chamber plate hall cathedral
gout reverb 3 plate w40              # a preset with your own wet level
gout reverb 3 off | on | clear
gout reverb 3                        # the decay drawn in dB over time
gout reverb master room w10          # or: gout set reverb room w10
```

ffmpeg has no algorithmic reverb, so gout builds one: it synthesises a stereo impulse response
(a few early reflections, then noise decaying to -60 dB over the decay time, its top end dying
faster the more damping) and convolves the track with it through ffmpeg's `afir`. The track is
summed to mono on the way in and the left and right responses are independent, so the reverb is
wide whatever the pan. The response has unit energy, so `w100` on a sustained sound is about as
loud as the dry signal. Responses are cached in `.gout/ir/` inside the project and rebuilt when
missing; they take a fraction of a second.

The reverb sums what reaches it to mono and returns it wide, so it is wide whatever comes before
it. The track's pan comes after the chain, as on a mixer strip, so a hard-panned track pans its
reverb too; put the reverb on the master chain for a shared, centred room. Its tail counts: stems
and the master run on until it has died away.

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
Hard trims and `rm -D` are the only things `undo` cannot take back, and undo stops there: changes
made before one of them can no longer be undone either.

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

## Tests

```sh
python3 -m unittest discover -s tests       # about a minute and a half
```

The suite runs the real command in temporary directories and measures the audio it writes with
ffmpeg: where a click lands after moves and trims, whether stems sum to the master, loudness
targets, effect behaviour, the sidecar round trip, and the ui's prompt and sheet. It never loads
your own addons. `GOUT_KEEP_TEST_DIRS=1` keeps the temporary projects for a look afterwards.

## Times and sizes

```
00:34:00        HH:MM:SS            34:00     MM:SS
00:34:00.500    HH:MM:SS.mmm        34        a bare number is MINUTES
00:34:00:500    HH:MM:SS:mmm        90s  2.5m  1.5h  500ms   explicit units

1.99            a bare number is MB           700MB  1.99GB  500kB   decimal
25MiB  1.99GiB  binary units
```

Use the explicit units for nudges: `move 2 +500ms`, not `move 2 +0.5`.

## License

gout is free software: you can redistribute it and change it under the terms of the GNU General
Public License, version 3 or (at your option) any later version. See `LICENSE`. Copyright 2026
Anomata Project. [CONTRIBUTING.md](CONTRIBUTING.md) says how to take part.
