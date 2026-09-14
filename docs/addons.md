# Writing an addon for gout

An addon is one Python file that gives gout a new effect or a new full-screen view. Its effect
becomes a command like the built-in `eq` and `reverb`: with presets, a place in `gout fx` chains,
a row in the parameter sheet, an entry in `gout.json`, and lines in help and the cheat sheet.

This guide builds a phaser from nothing, then covers the rest of what an addon can do.

## Where addons live

gout reads every `*.py` file in one folder:

| system | folder |
| --- | --- |
| Linux, macOS | `~/.config/gout/addons/` (or `$XDG_CONFIG_HOME/gout/addons/`) |
| Windows | `%APPDATA%\gout\addons\` |

`GOUT_ADDONS=/some/folder` points gout at another folder, which is handy while you work on one.
`gout addons` prints the folder and what loaded from it; `gout addons examples` copies the
examples that come with gout into it (chorus, distortion, saturation, tremolo and the fractal).

gout never loads addons from a project folder. An addon is ordinary Python that runs with your
permissions, so a project someone sends you can never run code. Read an addon before you put it
in your folder, as you would any program.

## Your first addon: a phaser

Save this as `phaser.py` in your addon folder:

```python
"""A phaser for gout: a sweeping notch that makes the sound swirl."""
import math
import re

from gout.fx import Effect


class Phaser(Effect):
    name = "phaser"
    aliases = ("ph",)
    summary = "phaser: a sweeping notch, rate in Hz, depth in %, delay in ms"
    syntax = "0.5hz d50 t3"
    hint = "0.5hz d50 t3 | slow | swirl | jet"
    presets = {
        "slow": ("0.2hz d40 t3", "a gentle, slow sweep"),
        "swirl": ("0.8hz d60 t3", "the classic swirl"),
        "jet": ("1.5hz d80 t1.5", "fast and deep"),
    }
    order = 32  # after eq, comp and delay, before reverb

    def parse(self, text):
        params = {"rate": 0.5, "depth": 50.0, "delay": 3.0}
        for word in text.lower().split():
            if re.fullmatch(r"\d+(\.\d+)?hz", word):
                params["rate"] = float(word[:-2])
            elif re.fullmatch(r"d\d+(\.\d+)?", word):
                params["depth"] = float(word[1:])
            elif re.fullmatch(r"t\d+(\.\d+)?", word):
                params["delay"] = float(word[1:])
            else:
                raise ValueError(f"bad phaser setting {word!r}; a line looks like  0.5hz d50 t3")
        if not 0.1 <= params["rate"] <= 2:
            raise ValueError("the rate is 0.1 to 2 Hz")
        if not 0 <= params["depth"] <= 90:
            raise ValueError("the depth is 0 to 90 %")
        if not 1.5 <= params["delay"] <= 5:
            raise ValueError("the delay is 1.5 to 5 ms")
        return params

    def format(self, params):
        return f"{params['rate']:g}hz d{params['depth']:g} t{params['delay']:g}"

    def filters(self, ctx, params):
        decay = params["depth"] / 100
        # the feedback adds energy, 1 / (1 - decay²) of it; in_gain 0.4 leaves room for the peaks.
        # out_gain takes both back out, so the phaser changes the colour and not the level.
        out_gain = math.sqrt(1 - decay * decay) / 0.4
        return [f"aphaser=in_gain=0.4:out_gain={out_gain:.3f}:delay={params['delay']:g}"
                f":decay={decay:.3f}:speed={params['rate']:g}:type=t"]


def register(gout):
    gout.requires(1)
    gout.add_effect(Phaser())
```

Then, in a project:

```sh
gout addons                  # phaser.py   phaser
gout phaser 2 swirl          # a phaser on track 2, with the swirl preset
gout phaser 2 1hz d70        # change it
gout phaser presets          # what the presets stand for
gout fx 2                    # the chain in order: a phaser goes after a delay, before a reverb
gout play
```

In the terminal ui the same commands work at the prompt, the parameter sheet (ctrl-e) has a row
for it, and tab completes `phaser` and its presets.

## What the parts do

**`register(gout)`** is the one function gout calls. `gout.add_effect(...)` adds an effect,
`gout.add_screen(...)` a full-screen view. `gout.requires(1)` says which version of the addon
API the file is written for (see [The addon API version](#the-addon-api-version)).

**The attributes** describe the effect to everything that shows it:

| attribute | what it is for |
| --- | --- |
| `name` | the command, and the kind stored in a chain and in `gout.json` |
| `aliases` | short names, like `ph` |
| `summary` | one line for help, `gout fx kinds` and the cheat sheet |
| `syntax` | an example settings line |
| `hint` | a short hint in the parameter sheet |
| `presets` | name → (settings line, what it is for) |
| `order` | where `gout phaser TRACK ...` puts a new one: before the first effect with a higher order (eq 10, comp 20, delay 30, reverb 40) |
| `empty` | how an empty settings line reads, `(defaults)` unless you say |
| `cheat`, `help` | extra cheat-sheet rows `(name, short, args, what)` and help lines |
| `picture_width`, `picture_height`, `legend` | for a picture, below |

**`parse(text)`** turns a settings line into a dict and raises `ValueError` with a message the
user can act on. Presets are already expanded when it runs, so it never sees `swirl`.
**`format(params)`** writes the line back; `parse(format(p))` must give `p` again, because the
line is what gout stores.

**`filters(ctx, params)`** returns ffmpeg audio filters for a stereo signal, applied in order.
Before them the track has been brought to the project's rate and to stereo; after them come the
track's gain and pan.

## Measure what it does

Settings that sound right on one file can be wrong on the next, so measure an effect on test audio
before trusting it. ffmpeg's `aphaser` with its default gains makes a sound about 10 dB quieter.
Measuring pink noise at depths from 0 to 90 % and delays from 0.5 to 5 ms showed why: the
feedback raises the energy by 1 / (1 − decay²), and `in_gain` lowers it by a fixed 8 dB. The
`out_gain` above cancels both. It keeps the level within 1 dB from 1.5 ms up, and short delays
drifted further, which is why the phaser starts at 1.5 ms.

The measuring is two ffmpeg commands:

```sh
ffmpeg -f lavfi -i "anoisesrc=d=8:c=pink:a=0.3" -ac 2 noise.wav
ffmpeg -i noise.wav -af "aphaser=in_gain=0.4:out_gain=1:decay=0.6,ebur128=framelog=quiet" -f null - 2>&1 | grep "I:"
```

## When a filter list is not enough

**`graph(ctx, params, src, out, inputs)`** replaces `filters` for effects that treat left and right
apart, blend a dry path back in, or read a file. It returns a filtergraph from the label `src` to
`[out]`; every label you make must start with `out`. An extra input file (an impulse response) is
appended to `inputs` and then read as `[N:a]`, where N is its index in that list. `chorus.py`
and `saturation.py` in `examples/addons/` are complete ones.

**`tail_ms(ctx, params)`** says how long the effect keeps sounding after its input ends, like a
delay's echoes. Tails decide where stems end and where the master's fade-out ends.

**`check(ctx, params)`** raises `gout.core.GoutError` when the project cannot run the settings:
the built-in delay does that for note values in a project without a bpm.

**`picture(ctx, params, width, height)`** draws the effect in the ui's effect panel and under
`gout phaser TRACK`. It returns rows of `(text, classes, kind)`: one `head` row, `height` rows of
kind `graph` and one `axis` row. The first 4 characters of each graph row (`gout.fx.GUTTER`) are
labels. `classes` has one letter per character: `a` for the curve (bold, in the theme's colour),
`z` and `x` for dim, a space for plain. `params` is `None` when the track has no such effect yet.
`tremolo.py` draws a second of its gain in thirty lines.

**`ctx`**, the `FxContext`, is what the effect may know: `ctx.rate` (the project's sample rate),
`ctx.bpm` (or `None`), `ctx.is_master`, `ctx.track`, `ctx.source()` (the track's file),
`ctx.chain_input()` (filters that turn that file into what the chain receives, for effects that
measure their own track), `ctx.peaks()`, `ctx.spectrum()` and `ctx.cache("name", "file")` (a path
in the project's `.gout/` cache).

**`shortcuts`** adds commands that edit the effect's line: `{"hp": (usage, summary, fn)}`, where
`fn(line, words)` returns the new line. The built-in eq's `hp` and `lp` are made that way.

## Screens

A screen fills the terminal while the song plays, and moves with it. `fractal.py` is one. The
smallest:

```python
from gout.screens import Screen


class Bars(Screen):
    name = "bars"
    key = "ctrl-b"
    summary = "the three bands as bars"

    def frame(self, ctx, width, height):
        rows = []
        for band in ("low", "mid", "high"):
            n = round(ctx.band(band) * width)
            rows.append(("#" * n, "0" * n))
        return rows


def register(gout):
    gout.requires(1)
    gout.add_screen(Bars())
```

`frame(ctx, width, height)` returns up to `height` rows of `(text, classes)`. A digit in `classes`
is a track colour from the theme, `m` the master's colour, a space the terminal's own.
`ctx.band("low")` is how loud a band is now (`low`, `mid`, `high`, `level`, `onset`, 0 to 1),
`ctx.travel("low")` how much of it has gone by (it only grows, faster when the band is busy),
`ctx.position_ms` and `ctx.playing` where the song is.

A screen opens with its key or its name at the prompt. The keys a screen may take are the ones
the ui does not use: `ctrl-space`, `ctrl-b`, `ctrl-o`, `ctrl-q`, `ctrl-r`, `ctrl-v`, `ctrl-y`. Esc
or the key again goes back; space and the left and right arrows play, stop and move as always.
Other keys go to `key_pressed(ctx, key)`, which returns `True` when it used the key. `fps`,
`status(ctx)`, `help` (rows for the cheat sheet), `fullscreen`, `status_seconds` and
`command(ctx, words)` (what was typed after the name) are there when you need them; the base
class in `gout/screens.py` documents each. `gout.screens.config_file("bars.json")` is the place
for a settings file, next to `color.json`.

## Rules gout keeps

- Names, short names and shortcuts are lowercase letters, digits, `-` and `_`, up to 16
  characters, starting with a letter. They must not be taken by a gout command, another effect,
  a screen, or the words `master all presets kinds add move clear on off rm`.
- No preset may be called `on`, `off`, `clear` or `none`: those words mean something to every
  effect command.
- An addon that fails to load, or wants a name that is taken, is reported on every command and
  skipped; gout keeps working. An effect a project uses but this machine lacks stays in the
  chain, marked `(not installed)`, and is left out of the mix with a warning.
- The gout you install with an installer carries its own Python. An addon can use the Python
  standard library and gout itself, but no packages from pip. A gout run from source can use
  whatever its Python has, but an addon meant for others should stick to the standard library.

## The addon API version

`gout.requires(N)` at the top of `register` tells an older gout to skip the addon with a message
("update gout") instead of failing somewhere inside it. `gout.version` is the version the running
gout offers.

Version **1** is everything in this guide: `add_effect` and `add_screen`; the `Effect` attributes
and methods above; `FxContext`; `Screen` and `ScreenContext`; `gout.fx.GUTTER`;
`gout.screens.config_file`; `gout.formula`. The version goes up when an addon can rely on
something new, and a change that would break addons written for an older version is avoided.

## Trying it out

- `GOUT_ADDONS=~/work/my-addons gout addons` loads just your folder and says why an addon did not
  load (a syntax error comes with its line number).
- `gout fx kinds` lists every effect with where it came from; `gout help` shows yours with the
  rest.
- `gout -v mix` prints the ffmpeg commands, so you can see your filters in the graph.
- Make a small project from generated audio (`ffmpeg -f lavfi -i sine=frequency=220:duration=4
  tone.wav`) and measure the render, as above.

## Sharing an addon

An addon is a single `.py` file: share it however you like, and people drop it into their addon
folder. gout is free software under the GNU General Public License, version 3 or later, and an
addon is a program written against gout, so share yours under the same licence (or one compatible
with it).
