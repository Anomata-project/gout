# gout

Cut a piece out of an `.mp3` — by time, by length, or by target file size.

Wraps `ffmpeg`; copies the audio stream by default, so cuts are instant and lossless.

## Install

Needs `ffmpeg` and `ffprobe` on `PATH`. Everything else is the Python standard library —
no dependencies, no virtualenv.

```sh
./install.sh              # symlinks ~/.local/bin/gout -> ./gout.py
```

Then `gout` works from any directory, on any file:

```sh
cd ~/Music && gout podcast.mp3 -st 34 -fs 1.99
gout /mnt/usb/show.mp3 -el 3 -o ~/clips/teaser.mp3
```

The symlink points back at this checkout, so editing `gout.py` takes effect immediately.
Pass a different target dir if you want it system-wide: `sudo ./install.sh /usr/local/bin`.

Other ways:

```sh
pipx install .            # isolated venv, copies the code
pip install --user -e .   # editable install (may need --break-system-packages)
./gout.py --help          # or just run the script in place
```

Paths are resolved against your current directory, and the default output lands next to the
input file — so nothing depends on where the code lives.

## Usage

```
gout INPUT [-o OUT] [-st TIME] [-et TIME | -el TIME | -fs SIZE]
```

| flag | meaning |
| --- | --- |
| `-st`, `--start` | start time, from the beginning of the file (default `0`) |
| `-et`, `--end` | absolute end time, from the beginning of the file |
| `-el`, `--length` | length of the cut, from the start time |
| `-fs`, `--file-size` | cut as much as fits in this file size |
| `-o`, `--output` | output path (default `<name>_cut.mp3`) |
| `-r`, `--reencode` | re-encode instead of copy — sample-accurate, slower |
| `-f`, `--force` | overwrite the output |
| `-n`, `--dry-run` | show what would be cut, write nothing |
| `-v`, `--verbose` | show the ffmpeg commands and each size-fitting pass |

`-et`, `-el` and `-fs` are mutually exclusive. With none of them, the cut runs to the end of the file.

### Times

```
00:34:00        HH:MM:SS
00:34:00.500    HH:MM:SS.mmm
00:34:00:500    HH:MM:SS:mmm   (milliseconds after a fourth colon)
34:00           MM:SS
34              a bare number is minutes
90s  2.5m  1.5h explicit units
```

### Sizes

```
1.99      a bare number is MB
700MB     1.99GB     500kB
25MiB     1.99GiB    binary units
```

`MB`/`GB` are decimal (1 MB = 1 000 000 B), so a `-fs 1.99` result stays under a 2 MB
limit no matter which convention the other end uses.

## Examples

```sh
gout show.mp3 -st 00:34:00 -fs 1.99          # 1.99 MB starting at 34:00
gout show.mp3 -st 12 -el 3                   # 3 minutes, starting 12 minutes in
gout show.mp3 -st 00:01:30 -et 00:04:05      # absolute in and out points
gout show.mp3 -el 00:00:30 -o teaser.mp3     # first 30 seconds
gout show.mp3 -st 45 -o rest.mp3             # from 45:00 to the end
```

## How `-fs` works

The first cut length is estimated from the stream bitrate, then the result is measured and
retried until it lands between 97 % and 100 % of the limit (up to six passes, usually one).
The output is never larger than the limit. Verbose mode shows each pass:

```
$ gout show.mp3 -fs 1.0 -v
  pass 1: 00:01:20.503 -> 743.79 kB (74.4% of limit)
  pass 2: 00:01:48.017 -> 997.37 kB (99.7% of limit)
```

## Notes

- Stream copy snaps the cut to MP3 frame boundaries (~26 ms). Pass `-r` if you need the
  cut exactly on the requested millisecond.
- Tags are carried over (`-map_metadata`), and a Xing header is written so players report
  the right duration.
