"""PortAudio through ctypes: one stream that plays and records at once, for recording along with
the project.

Only the blocking calls are used (Pa_WriteStream, Pa_ReadStream and the two Available counts):
ctypes lets go of the GIL inside them. A Python callback from PortAudio's audio thread starves as
soon as another Python thread in the process is busy, and so does a blocking loop sharing a
process with busy threads, which is why gout runs the loop in a process of its own (engine.py).

The library comes from next to a packaged gout (portaudio/ beside the program, as ffmpeg/ is),
from GOUT_PORTAUDIO (a path, or none to act as if it were missing), or from the system.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .core import die, frozen

FLOAT32 = 0x00000001
INPUT_OVERFLOWED, OUTPUT_UNDERFLOWED = -9981, -9980
INVALID_SAMPLE_RATE = -9997


class DeviceInfo(ctypes.Structure):
    _fields_ = [("structVersion", ctypes.c_int), ("name", ctypes.c_char_p), ("hostApi", ctypes.c_int),
                ("maxInputChannels", ctypes.c_int), ("maxOutputChannels", ctypes.c_int),
                ("defaultLowInputLatency", ctypes.c_double), ("defaultLowOutputLatency", ctypes.c_double),
                ("defaultHighInputLatency", ctypes.c_double), ("defaultHighOutputLatency", ctypes.c_double),
                ("defaultSampleRate", ctypes.c_double)]


class HostApiInfo(ctypes.Structure):
    _fields_ = [("structVersion", ctypes.c_int), ("type", ctypes.c_int), ("name", ctypes.c_char_p),
                ("deviceCount", ctypes.c_int), ("defaultInputDevice", ctypes.c_int),
                ("defaultOutputDevice", ctypes.c_int)]


class StreamParameters(ctypes.Structure):
    _fields_ = [("device", ctypes.c_int), ("channelCount", ctypes.c_int), ("sampleFormat", ctypes.c_ulong),
                ("suggestedLatency", ctypes.c_double), ("hostApiSpecificStreamInfo", ctypes.c_void_p)]


@dataclass
class Device:
    index: int
    name: str
    host_api: str
    inputs: int
    outputs: int
    rate: float
    high_input_latency: float
    high_output_latency: float


_lib: ctypes.CDLL | None = None
_tried = False


def library_candidates() -> list[str]:
    wanted = os.environ.get("GOUT_PORTAUDIO", "").strip()
    if wanted:
        return [] if wanted == "none" else [wanted]
    out: list[str] = []
    if frozen():
        folder = Path(sys.executable).resolve().parent / "portaudio"
        if folder.is_dir():
            out += [str(p) for p in sorted(folder.iterdir()) if p.suffix in (".dll", ".dylib", ".so") or ".so." in p.name]
    found = ctypes.util.find_library("portaudio")
    if found:
        out.append(found)
    if sys.platform == "darwin":
        out += ["/opt/homebrew/lib/libportaudio.dylib", "/usr/local/lib/libportaudio.dylib"]
    elif os.name == "nt":
        out += ["libportaudio64bit.dll", "portaudio_x64.dll", "portaudio.dll"]
    else:
        out += ["libportaudio.so.2"]
    return out


def library() -> ctypes.CDLL | None:
    """The loaded library with its function signatures set, or None when there is none."""
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for name in library_candidates():
        try:
            lib = ctypes.CDLL(name)
            declare(lib)
        except (OSError, AttributeError):
            continue
        _lib = lib
        break
    return _lib


def declare(lib: ctypes.CDLL) -> None:
    stream = ctypes.c_void_p
    signatures = {
        "Pa_Initialize": ([], ctypes.c_int), "Pa_Terminate": ([], ctypes.c_int),
        "Pa_GetVersionText": ([], ctypes.c_char_p), "Pa_GetErrorText": ([ctypes.c_int], ctypes.c_char_p),
        "Pa_GetDeviceCount": ([], ctypes.c_int), "Pa_GetDeviceInfo": ([ctypes.c_int], ctypes.POINTER(DeviceInfo)),
        "Pa_GetHostApiInfo": ([ctypes.c_int], ctypes.POINTER(HostApiInfo)),
        "Pa_GetDefaultInputDevice": ([], ctypes.c_int), "Pa_GetDefaultOutputDevice": ([], ctypes.c_int),
        "Pa_IsFormatSupported": ([ctypes.POINTER(StreamParameters), ctypes.POINTER(StreamParameters), ctypes.c_double],
                                 ctypes.c_int),
        "Pa_OpenStream": ([ctypes.POINTER(stream), ctypes.POINTER(StreamParameters), ctypes.POINTER(StreamParameters),
                           ctypes.c_double, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "Pa_StartStream": ([stream], ctypes.c_int), "Pa_AbortStream": ([stream], ctypes.c_int),
        "Pa_CloseStream": ([stream], ctypes.c_int),
        "Pa_ReadStream": ([stream, ctypes.c_void_p, ctypes.c_ulong], ctypes.c_int),
        "Pa_WriteStream": ([stream, ctypes.c_void_p, ctypes.c_ulong], ctypes.c_int),
        "Pa_GetStreamReadAvailable": ([stream], ctypes.c_long),
        "Pa_GetStreamWriteAvailable": ([stream], ctypes.c_long),
    }
    for name, (args, result) in signatures.items():
        func = getattr(lib, name)
        func.argtypes, func.restype = args, result


def available() -> bool:
    return library() is not None


def version() -> str:
    lib = library()
    return lib.Pa_GetVersionText().decode(errors="replace") if lib else ""


def error_text(code: int) -> str:
    lib = library()
    return lib.Pa_GetErrorText(code).decode(errors="replace") if lib else f"error {code}"


def initialize() -> ctypes.CDLL:
    """Pa_Initialize, with the ALSA and JACK chatter it prints on Linux kept off the terminal."""
    lib = library()
    if lib is None:
        die("PortAudio is not installed")
    quiet = os.name == "posix"
    if quiet:
        sys.stderr.flush()
        saved, null = os.dup(2), os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
    try:
        code = lib.Pa_Initialize()
    finally:
        if quiet:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(null)
    if code < 0:
        die(f"PortAudio did not start: {error_text(code)}")
    return lib


def devices(lib: ctypes.CDLL) -> list[Device]:
    out = []
    for index in range(lib.Pa_GetDeviceCount()):
        info = lib.Pa_GetDeviceInfo(index).contents
        api = lib.Pa_GetHostApiInfo(info.hostApi).contents
        out.append(Device(index, info.name.decode(errors="replace"), api.name.decode(errors="replace"),
                          info.maxInputChannels, info.maxOutputChannels, info.defaultSampleRate,
                          info.defaultHighInputLatency, info.defaultHighOutputLatency))
    return out


class Stream:
    """A blocking duplex stream of 32-bit float frames."""

    def __init__(self, lib: ctypes.CDLL, input_device: Device, input_channels: int, output_device: Device,
                 rate: int, block: int):
        self.lib, self.block = lib, block
        self.input_channels = input_channels
        self.params_in = StreamParameters(input_device.index, input_channels, FLOAT32, input_device.high_input_latency, None)
        self.params_out = StreamParameters(output_device.index, 2, FLOAT32, output_device.high_output_latency, None)
        self.rate = rate
        if lib.Pa_IsFormatSupported(ctypes.byref(self.params_in), ctypes.byref(self.params_out), rate) == INVALID_SAMPLE_RATE:
            self.rate = int(input_device.rate)  # e.g. WASAPI in shared mode: the device's own rate
        self.handle = ctypes.c_void_p()
        self.check(lib.Pa_OpenStream(ctypes.byref(self.handle), ctypes.byref(self.params_in),
                                     ctypes.byref(self.params_out), self.rate, block, 0, None, None),
                   f"could not open {input_device.name} and {output_device.name} at {self.rate} Hz")

    def check(self, code: int, what: str) -> int:
        if code < 0 and code not in (INPUT_OVERFLOWED, OUTPUT_UNDERFLOWED):
            die(f"{what}: {error_text(code)}")
        return code

    def start(self) -> None:
        self.check(self.lib.Pa_StartStream(self.handle), "could not start the stream")

    def write(self, address: int) -> bool:
        """Write one block from address; False when the output ran dry before it (a dropout)."""
        return self.check(self.lib.Pa_WriteStream(self.handle, address, self.block), "playing") == 0

    def read(self, address: int) -> bool:
        """Read one block into address; False when input was lost before it (a dropout)."""
        return self.check(self.lib.Pa_ReadStream(self.handle, address, self.block), "recording") == 0

    def read_available(self) -> int:
        return max(0, self.lib.Pa_GetStreamReadAvailable(self.handle))

    def write_available(self) -> int:
        return max(0, self.lib.Pa_GetStreamWriteAvailable(self.handle))

    def close(self) -> None:
        if self.handle:
            self.lib.Pa_AbortStream(self.handle)
            self.lib.Pa_CloseStream(self.handle)
            self.handle = ctypes.c_void_p()
