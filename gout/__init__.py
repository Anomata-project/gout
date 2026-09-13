"""gout — a command-line DAW. Stack wav/mp3 tracks on a timeline, mix to master.wav.

    gout new song && cd song
    gout add drums.mp3 bass.wav        # copied into master/, master.wav is mixed
    gout move 2 +1.5s                  # nudge the bass 1.5 s later, mix again
    gout cut show.mp3 -st 34 -fs 1.99  # the 1.x cutter, unchanged
"""
from .core import __version__

__all__ = ["__version__"]
