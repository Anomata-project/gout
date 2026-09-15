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
                                  cheat sheet right, ctrl-t / ctrl-k hide each; ctrl-u undoes; ctrl-e opens the
                                  parameter sheet: every setting and track parameter as name, value
                                  and new value, ctrl-s applies, the highlighted row shown as a command
                                  to copy, ctrl-p puts it on the prompt); elsewhere: this page. The prompt
                                  edits like a shell: arrows, home/end, up/down for earlier commands
                                  (kept per project), tab completes commands, tracks, presets and file
                                  names from where gout was started, right arrow takes the grey
                                  suggestion after the cursor; ctrl-o (or window) opens the timeline in a
                                  browser window, in high definition
  gout play  pl [FROM] [-r]       play from FROM (1:30, 45s): master.wav when it matches the project,
                                  otherwise the project streamed live, effects and all, at once
                                  (-r renders first instead); ctrl-c stops. In the ui: space on an
                                  empty line plays and stops, left/right there move the playhead 5 s,
                                  play FROM / stop work at the prompt. Player: ffplay, else pw-cat,
                                  paplay or aplay; GOUT_PLAYER picks one (null plays in silence)
  gout loop  lo FROM TO | on | off  play a stretch of the song over and over: space in the ui and
                                  gout play go round it, from its start or from inside it to its end,
                                  without a gap (ffmpeg's aloop). The ruler shows it thick. loop alone
                                  says what it is; a take does not loop
  gout record rec [FROM] [-t LENGTH] [-n NAME] [-i INPUT] [-c N] [-s] [-d] [-M]
                                  record from an input into a new track at FROM (default 0:00) while
                                  the project plays from there: a 32-bit float wav in {TRACK_DIR}/ called
                                  rec, or NAME. ctrl-c stops, -t stops after LENGTH. Mono from channel 1
                                  of the input, -c N from channel N, -s stereo from N and N+1. The take
                                  lines up with what played: the latency is measured every take and
                                  soft-trimmed off, to within half a millisecond once calibrated. -d
                                  records without playing; so does gout without PortAudio (gout version
                                  says). -i records from another input this once (gout inputs lists
                                  them). You hear the input in your headphones as you record (PipeWire,
                                  Linux; not through the speakers); -M does not. A muted input is said
                                  before the take. undo deletes the take; a take a crash cut short is
                                  still a file, and gout scan registers it. In the ui, ctrl-r records
                                  from the playhead and ctrl-r or space stops; record and record check
                                  work at its prompt too (calibrate runs from a shell)
  gout record check [FROM] [-t LENGTH] [-i INPUT] [-c N] [-s] [-d] [-M]
                                  a rehearsal: the song plays, you hear yourself, a meter shows the
                                  level, the loudest peak and the clips; at the end what to turn up or
                                  down. Nothing is kept
  gout record calibrate [-i INPUT] [-c N]  play 10 clicks and record them, once per input and output
                                  (headphones and speakers count as different outputs): the microphone
                                  near the speaker, or a cable from the output to the input. Kept for
                                  this computer; every later take is lined up with it
  gout inputs in [N | NAME | default]  what gout can record from here, and on Linux what each output
                                  plays; N or NAME picks one for this computer, default goes back to
                                  the system's. Capture: pw-record, parecord or arecord (Linux), ffmpeg
                                  (macOS, Windows); GOUT_RECORDER picks one (null records silence)
  gout video vd IMAGE | SCREEN [CHOICE... | all] [-e 10s] [-c IMAGE] [-T] [-o FILE]
                                  an mp4 for YouTube with {MASTER_WAV} (rendered first when out of date):
                                  an image fitted with black bars, or a screen moving with the song,
                                  frame by frame: gout video fractal 3 1 0, gout video fractal all, or
                                  gout video zoom 3 1 0 (an endless zoom into the same fractal)
                                  (a new choice every 10 s or -e, cut on the nearest drum hit); -c shows
                                  a cover for the first 6 s. The title and artist (set title, set artist)
                                  come up big at the start; what follows a dash in the title goes under
                                  it; -T leaves them out. 1920x1080 25 fps, H.264 and AAC 320k, to
                                  master.mp4 unless -o. A fractal video takes about 3 times the song's
                                  length on 4 cores, a zoom up to half as long again
  gout view  v                    print the timeline once: the master first, then each track as a
                                  waveform (braille dots from the real highest and lowest points)
  gout colors [--init [--project]]  the 25 colour and layout settings from color.json: which file is in
                                  use, what is wrong with it, every value; --init writes the defaults
                                  to ~/.config/gout/color.json (--project: into this project)
  gout cheat c                    print the cheat sheet
  gout ls    l                    list the tracks and the state of {MASTER_WAV}
  gout mix   x  [-3] [-v]         render {MASTER_WAV} (32-bit float stereo); -3 also writes {MASTER_MP3}
  gout undo  u                    undo the last change, again for the one before (ctrl-u in the ui);
                                  not past a hard trim or rm -D
  gout saveas sa NAME | PATH      copy the whole project (files included) next to this one, or to PATH
  gout stems sm [DIR] [-A]        one wav per track, trimmed, placed, gained and panned as in the mix,
                                  all the same length from 0:00, into {STEMS_DIR}/ (-A: only what the mix hears)
  gout dump  dp                   print the project state as JSON: the same document gout keeps in
                                  {SIDECAR} next to the database, rewritten after every change
  gout import im FILE.json [-s|-t]  apply such a document: settings, and tracks matched by file name
                                  (-s settings only, e.g. a master template; -t tracks only)
  gout rebuild rb [-f]            recreate {DB_NAME} from the files in {TRACK_DIR}/, then restore positions,
                                  trims and settings from {SIDECAR} when it is there
  gout set   se KEY VALUE         settings; gout set alone lists them:  rate HZ, and autorender idle|on|off:
                                  idle (default) changes are instant and the ui renders master.wav when
                                  nothing has changed for a moment; on renders after every change;
                                  off only when you mix. Out of date, play streams the project live.
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
  gout move  m  TRACK PART +TIME | TIME      a part along its track: move 3 p2 +1s, move 3 chorus 1:30
  gout move  m  TRACK... | all +TIME | TIME  several tracks, or all: by the same amount, or the
                                             earliest placed at TIME with the spacing kept
  gout trim  t  TRACK [-st T] [-et T|-el T]  soft trim: in/out points, the file is untouched;
                                             -et -5s is 5 s before the file's end
  gout trim  t  TRACK PART [-st T] [-et T]   a part: where it is heard from and until, in timeline time;
                                             +200ms or -1s moves that edge from where it is
  gout trim  t  TRACK -c                     soft trim off again (--clear)
  gout trim  t  TRACK -H [-st ..] [-et ..]   hard trim: rewrite the file, bakes the soft trim (--hard)
  gout rm    r  TRACK [-D]                   drop a track; -D also deletes its file (--delete)
  gout rm    r  TRACK PART                   drop a part; the file and the other parts stay
  gout mute  mu TRACK [PART] [on|off]        toggle mute        (mute all off)
  gout solo  s  TRACK [on|off]               toggle solo        (solo all off)
  gout gain  g  TRACK [PART] DB              gain 2 -6, gain 2 p3 -6
  gout pan   p  TRACK [PART] C | L30 | R30   balance; every track starts centred, 50/50
  gout part  pt TRACK [TIME... | PART name [NAME] | join [PART PART] [-f]]
                                             cut a track into parts where you hear TIME (at the ui's prompt,
                                             part 3 here cuts at the playhead); the parts stay on the track.
                                             They are p1, p2 ... from the left, or the name you give. gain,
                                             pan, mute, fx and every effect take a part after the track:
                                             gain 3 p2 -10, eq 3 chorus hp80, fx 3 p2 add reverb. Parts
                                             meet with a 10 ms crossfade, so a cut alone changes nothing you
                                             hear. join makes one piece again, or joins two neighbours;
                                             parts with settings of their own need -f, which drops them.
                                             A part moves along its track (move 3 p2 +1s), trims where you
                                             hear it (trim 3 p2 -st 1:31 -et +2s) and goes (rm 3 p2). An edge
                                             that no longer meets another part fades over 5 ms. part TRACK
                                             alone lists them
{{EFFECTS}}  -N (--no-mix) on any of these skips the automatic re-mix; -p DIR before a command picks
  the project. Long flags: --at --name --hard --clear --reencode --delete --mp3 --rate --width

ADDONS
  gout addons           where addons are read from, what loaded, and effects this project lacks
  gout addons examples  copy the example addons that come with gout into that folder
                        The folder is ~/.config/gout/addons (%APPDATA%\\gout\\addons on Windows;
                        $XDG_CONFIG_HOME or $GOUT_ADDONS when set). An addon is a .py file with an Effect
                        class and register(gout); it becomes a command, a row in the sheet and an entry
                        in gout fx kinds. examples/addons/tremolo.py is one; docs/addons.md tells how.
                        An addon can also add a full-screen view to the ui: examples/addons/fractal.py
                        opens with ctrl-space (esc goes back) and moves with the music.
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
  Trim times count from the start of the track's own file, not from the timeline; with a
  minus they count back from its end: trim 2 -et -5s leaves out the last 5 seconds.
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
        "  gout KIND     TRACK [PART] SETTINGS | PRESET  the first effect of that kind (on a part: eq 3 p2 hp80); added where it",
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


def help_for(word: str) -> list[str]:
    """The instruction page's entries for one command, by its long or short name: gout help record."""
    word = word.lower()
    found: list[str] = []
    taking = False
    for line in help_text().splitlines():
        if line.startswith("  gout "):
            names = line.split()[1:3]
            taking = word == names[0] or (len(names) > 1 and word == names[1] and names[1].islower())
        elif not line.startswith("    "):
            taking = False  # a blank line or the next heading ends an entry
        if taking:
            found.append(line)
    return found
