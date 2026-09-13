#!/usr/bin/env bash
# Put `gout` on your PATH. The symlink points back at this checkout, so edits
# to the gout/ package take effect immediately — no reinstall.
#
#   ./install.sh              -> ~/.local/bin/gout
#   ./install.sh /usr/local/bin  (or BINDIR=... ./install.sh)
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bin/gout"
bindir="${1:-${BINDIR:-$HOME/.local/bin}}"

command -v ffmpeg  >/dev/null || echo "warning: ffmpeg not found on PATH"  >&2
command -v ffprobe >/dev/null || echo "warning: ffprobe not found on PATH" >&2

mkdir -p "$bindir"
chmod +x "$src"
ln -sfn "$src" "$bindir/gout"
echo "linked $bindir/gout -> $src"

case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "note: $bindir is not on your PATH; add this to your shell rc:" >&2
     echo "      export PATH=\"$bindir:\$PATH\"" >&2 ;;
esac

"$bindir/gout" --version
