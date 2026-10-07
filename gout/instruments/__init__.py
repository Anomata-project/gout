"""The built-in instruments. Each module defines one Instrument subclass; addons have the same shape."""


def builtin() -> list:
    from .synth import Synth
    found = [Synth()]
    try:
        from .bonepipe import BonePipe
        found.append(BonePipe())
    except ImportError:  # gout without the bonepipe package beside it: one instrument fewer
        pass
    return found
