"""The hand-written instruction page."""
from __future__ import annotations

from .core import __version__, DB_NAME, DEFAULT_RATE, MASTER_MP3, MASTER_WAV, SIDECAR, STEMS_DIR, TRACK_DIR
from .fx import effects


HELP_TEMPLATE = f"""\
gout {__version__} — a command-line DAW. Stack wav/mp3 tracks on a timeline, mix to master.wav.

Every command has a long and a short name (gout add / gout a). gout cheat prints the sheet.

PROJECT
  gout new   n  NAME [-R HZ]      create NAME/ with {TRACK_DIR}/ and {DB_NAME} (default {DEFAULT_RATE} Hz)
  gout                            inside a project: open the terminal ui (prompt left; timeline and
                                  cheat sheet right, ctrl-u / ctrl-k hide each; ctrl-e opens the
                                  parameter sheet: every setting and track parameter as name, value
                                  and new value, ctrl-s applies); elsewhere: this page. The prompt
                                  edits like a shell: arrows, home/end, up/down for earlier commands
                                  (kept per project), tab completes commands, tracks, presets and file
                                  names from where gout was started, right arrow takes the grey
                                  suggestion after the cursor
  gout view  v                    print the timeline once: one row per track, each column a
                                  block ▁▂▃▄▅▆▇█ as tall as the peak there (6 dB per step)
  gout cheat c                    print the cheat sheet
  gout ls    l                    list the tracks and the state of {MASTER_WAV}
  gout mix   x  [-3] [-v]         render {MASTER_WAV} (32-bit float stereo); -3 also writes {MASTER_MP3}
  gout undo  u                    undo the last change (not a hard trim or rm -D)
  gout saveas sa NAME | PATH      copy the whole project (files included) next to this one, or to PATH
  gout stems sm [DIR] [-A]        one wav per track, trimmed, placed, gained and panned as in the mix,
                                  all the same length from 0:00, into {STEMS_DIR}/ (-A: only what the mix hears)
  gout dump  dp                   print the project state as JSON: the same document gout keeps in
                                  {SIDECAR} next to the database, rewritten after every change
  gout import im FILE.json [-s|-t]  apply such a document: settings, and tracks matched by file name
                                  (-s settings only, e.g. a master template; -t tracks only)
  gout rebuild rb [-f]            recreate {DB_NAME} from the files in {TRACK_DIR}/, then restore positions,
                                  trims and settings from {SIDECAR} when it is there
  gout set   se KEY VALUE         settings; gout set alone lists them:  autorender on|off,  rate HZ
  gout stats st                   integrated LUFS, LRA and true peak per track file, and for {MASTER_WAV}

MASTER   (gout set KEY VALUE)
  lufs -14 | off        loudness target. Two passes of ffmpeg's loudnorm: a plain gain change
                        whenever the ceiling allows, otherwise dynamic, and the mix line says which.
                        -14 streaming (Spotify, YouTube), -16 Apple Music and podcasts, -23 broadcast
  ceiling -1            true-peak ceiling in dBTP for that step (default -1)
  effects               the master has an effect chain like a track: gout fx master ...,
                        gout eq master hp30, gout reverb master room (also: gout set eq hp30)
  bpm 120               the tempo, so delay times can be note values
  gain -3               master gain in dB before the loudness step
  fadein 500ms          fades on the sum;  fadeout 3s
  head 500ms  tail 2s   silence padded before and after
  bits 32f | 24 | 16    {MASTER_WAV} format (16 is dithered);  mp3 320k | 192k | v0  bounce quality
  title artist album year comment   tags written into {MASTER_WAV} and {MASTER_MP3}
  Every mix line reports the result:  mix   master.wav  03:12.500  -14.0 LUFS  LRA 6.2  peak -1.0 dBTP

TRACKS   (TRACK is the number shown by ls, or the track name)
  gout add   a  FILE... [-n NAME] [-a TIME]  copy wav/mp3 into {TRACK_DIR}/ (other formats become wav)
  gout scan  sc                              register wav/mp3 you copied into {TRACK_DIR}/ yourself,
                                             at 0; reports tracks whose file has gone missing
  gout move  m  TRACK +TIME | -TIME | TIME   nudge later, nudge earlier, or place at a time
  gout trim  t  TRACK [-st T] [-et T|-el T]  soft trim: in/out points, the file is untouched
  gout trim  t  TRACK -c                     soft trim off again (--clear)
  gout trim  t  TRACK -H [-st ..] [-et ..]   hard trim: rewrite the file, bakes the soft trim (--hard)
  gout rm    r  TRACK [-D]                   drop a track; -D also deletes its file (--delete)
  gout mute  mu TRACK [on|off]               toggle mute        (mute all off)
  gout solo  s  TRACK [on|off]               toggle solo        (solo all off)
  gout gain  g  TRACK DB                     gain 2 -6
  gout pan   p  TRACK C | L30 | R30          balance; every track starts centred, 50/50
{{EFFECTS}}  -N (--no-mix) on any of these skips the automatic re-mix; -p DIR before a command picks
  the project. Long flags: --at --name --hard --clear --reencode --delete --mp3 --rate --width

ADDONS
  gout addons           where addons are read from, what loaded, and effects this project lacks
                        The folder is ~/.config/gout/addons ($XDG_CONFIG_HOME, or $GOUT_ADDONS). An addon
                        is a .py file with an Effect class and register(gout); it becomes a command, a
                        row in the sheet and an entry in gout fx kinds. examples/addons/tremolo.py is one.
                        Addons are plain Python with your permissions; gout never loads them from projects.

CUT   (any file, no project needed; `gout INPUT ...` still works as in 1.x)
  gout cut INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE] [-r] [-f] [-n] [-v]
  -st start   -et absolute end   -el length   -fs largest piece that fits the size
  -o output (default <name>_cut.mp3)   -r re-encode for an exact cut   -f overwrite   -n dry run

TIME FORMATS
  00:34:00              HH:MM:SS
  00:34:00.500          HH:MM:SS.mmm      milliseconds after a dot
  00:34:00:500          HH:MM:SS:mmm      milliseconds after a fourth colon
  34:00                 MM:SS
  34                    a bare number is MINUTES, so -st 34 is 34 minutes in
  90s   2.5m   1.5h     explicit units (ms, s, m, h)  — use these for nudges: move 2 +500ms

SIZE FORMATS
  1.99                  a bare number is MB
  700MB  1.99GB  500kB  decimal units, 1 MB = 1 000 000 bytes
  25MiB  1.99GiB        binary units,  1 MiB = 1 048 576 bytes

EXAMPLES
  gout new song && cd song
  gout add drums.mp3 bass.wav              two tracks at 0, {MASTER_WAV} mixed
  gout add vocals.wav --at 00:00:08        a track placed 8 s in
  gout move 3 +250ms                       nudge it a quarter second later
  gout solo 3 && gout mix --mp3            hear it alone, bounce an mp3
  gout cut show.mp3 -st 00:34:00 -fs 1.99  1.99 MB of audio starting at 34:00

SOFT AND HARD TRIM
  Trim times count from the start of the track's own file, not from the timeline.
  A soft trim only stores in/out points; the mix applies them sample-exactly and
  you can change them as often as you like. A hard trim rewrites the file in {TRACK_DIR}/
  so the material gets lighter; with no times it bakes the current soft trim. The
  sound stays where it was on the timeline. mp3 hard trims copy whole frames (26 ms
  grid, nothing re-encoded); -r re-encodes for the exact millisecond. wav is exact.

HOW THE MIX WORKS
  Every track is decoded, trimmed, delayed to its position, panned to stereo and summed
  with ffmpeg's amix (normalize off, so adding a track never turns the others down).
  {MASTER_WAV} is 32-bit float, so a hot sum cannot clip there; the peak is reported.

HOW -fs WORKS
  The length is estimated from the bitrate, cut, then measured and retried until the
  file lands between 97% and 100% of the limit — never over it.

NOTES
  Needs ffmpeg and ffprobe on PATH. gout cut on an mp3 copies the stream, which starts
  up to ~0.1 s late (ffmpeg's seek); -r re-encodes for the exact millisecond. wav is exact.
  Everything else is the Python standard library — the project state lives in {DB_NAME}
  (sqlite), the audio in {TRACK_DIR}/ is never modified by moves or trims.
"""


def effects_help() -> str:
    lines = [
        "",
        "EFFECTS   (a chain per track and one for the master; TRACK can be master. The audio goes",
        "          through the effects in order, then the track's gain and pan.)",
        "  gout fx    f  TRACK                      the chain, numbered;  gout fx kinds  lists every effect",
        "  gout fx    f  TRACK add KIND [SETTINGS]  add an effect at the end",
        "  gout fx    f  TRACK N SETTINGS | on | off | rm   change, bypass or remove slot N",
        "  gout fx    f  TRACK N move M             move slot N to position M",
        "  gout fx    f  TRACK clear                remove them all",
        "  gout KIND     TRACK SETTINGS | PRESET    the first effect of that kind; added where it",
        "                                           usually goes when the track has none",
        "  gout KIND     TRACK on | off | clear     bypass, bring back, remove;  gout KIND TRACK shows it",
        "  gout KIND     presets                    what the presets are",
        "",
    ]
    for eff in effects().values():
        short = eff.aliases[0] if eff.aliases else ""
        origin = "" if eff.source == "built-in" else f"   [addon: {eff.source}]"
        entry = f"gout {eff.name} {short}".rstrip()
        lines.append(f"  {entry:<18} TRACK SETTINGS        {eff.summary}{origin}")
        lines.append(f"                                           e.g.  gout {eff.name} 3 {eff.syntax}")
        for extra in eff.help:
            lines.append(f"                                           {extra}")
        if eff.presets:
            lines.append(f"                                           presets: {' '.join(eff.presets)}")
        for name, (usage, summary, _) in eff.shortcuts.items():
            lines.append(f"  gout {name:<10}TRACK {usage:<27} {summary}")
    return "\n".join(lines) + "\n"


def help_text() -> str:
    """The instruction page, with the effects there are (addons included)."""
    return HELP_TEMPLATE.replace("{EFFECTS}", effects_help(), 1)
