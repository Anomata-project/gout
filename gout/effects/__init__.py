"""The built-in effects. Each module defines one Effect subclass; addons have the same shape."""


def builtin() -> list:
    from .eq import EqEffect
    from .comp import CompEffect
    from .delay import DelayEffect
    from .reverb import ReverbEffect
    return [EqEffect(), CompEffect(), DelayEffect(), ReverbEffect()]
