"""The built-in instruments. Each module defines one Instrument subclass; addons have the same shape."""


def builtin() -> list:
    from .synth import Synth
    return [Synth()]
