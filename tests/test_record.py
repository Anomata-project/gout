import json
import math
import os
import signal
import subprocess
import time
import unittest

from helpers import duration, ffmpeg, gout_attr, gout_cmd, GoutTest, peak_time, samples, stream_info


def tone_level(values, freq: float, rate: int = 48000) -> float:
    """Amplitude of one sine frequency in the values."""
    w = 2 * math.pi * freq / rate
    s = sum(v * math.sin(w * i) for i, v in enumerate(values))
    c = sum(v * math.cos(w * i) for i, v in enumerate(values))
    return 2 * math.hypot(s, c) / max(1, len(values))


class RecordTest(GoutTest):
    def left_right(self):
        """1.5 s of stereo: 440 Hz on the left, 1000 Hz on the right."""
        path = self.tmp / "lr.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=440:d=1.5", "-f", "lavfi", "-i", "sine=1000:d=1.5", "-filter_complex",
               "[0][1]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR", "-c:a", "pcm_s16le", str(path))
        return path

    def test_a_take_becomes_a_track_where_it_started_and_undo_deletes_it(self):
        root = self.project("song")
        self.recorder = f"file:{self.fx / 'click.wav'}"  # 4 s, the click at 2.000 s, 44.1 kHz mono
        out = self.gout("record", "1s", "-t", "3s").stdout
        self.assertIn("stops after", out)
        track = self.dump()["tracks"][0]
        self.assertEqual((track["name"], track["offset_ms"], track["length_ms"], track["channels"]), ("rec", 1000, 3000, 1))
        take = root / "master" / "rec.wav"
        info = stream_info(take)
        self.assertEqual((info["codec_name"], info["channels"], info["sample_rate"]), ("pcm_f32le", 1, "48000"))
        self.assertAlmostEqual(duration(take), 3.0, places=3)  # -t stops on the sample
        self.assertAlmostEqual(peak_time(take), 2.0, delta=0.002)
        self.assertIn("peak", out)
        again = self.gout("rec", "-n", "vocal", "-t", "0.5s").stdout  # short name; a name of its own
        self.assertIn("vocal", again)
        self.gout("undo")
        self.assertFalse((root / "master" / "vocal.wav").exists())
        self.gout("undo")
        self.assertFalse(take.exists())
        self.assertEqual(self.dump()["tracks"], [])

    def test_channels_are_picked_not_mixed_and_the_take_ends_with_the_input(self):
        root = self.project("song")
        self.recorder = f"file:{self.left_right()}"
        self.gout("record", "-c", "2", "-n", "right")  # no -t: the file ending ends the take
        (right,) = samples(root / "master" / "right.wav", 1)
        self.assertAlmostEqual(len(right) / 48000, 1.5, delta=0.05)
        self.assertLess(tone_level(right[:24000], 440), 0.001)
        self.assertGreater(tone_level(right[:24000], 1000), 0.1)
        self.gout("record", "-s", "-n", "both")
        left, right = samples(root / "master" / "both.wav", 2)
        self.assertGreater(tone_level(left[:24000], 440), 0.1)
        self.assertLess(tone_level(left[:24000], 1000), 0.001)
        self.assertGreater(tone_level(right[:24000], 1000), 0.1)
        self.assertIn("no channel 3", self.gout("record", "-c", "3", ok=False).stderr)
        self.assertIn("no channel 3", self.gout("record", "-s", "-c", "2", ok=False).stderr)
        self.assertIn("-c takes", self.gout("record", "-c", "0", ok=False).stderr)
        self.assertEqual(len(self.dump()["tracks"]), 2)  # the failures left nothing behind
        self.assertEqual(sorted(p.name for p in (root / "master").iterdir()), ["both.wav", "right.wav"])

    def test_the_ui_sends_record_to_the_shell(self):
        root = self.project("song")
        from test_ui import FakeScreen
        os.environ["GOUT_ADDONS"] = str(self.addons)
        ui = gout_attr("tui", "Tui")(gout_attr("project", "Project")(root), FakeScreen(30, 120))
        ui.input = "record"
        ui.submit()
        self.assertIn("record: run that from the shell", ui.log[-1])

    @unittest.skipIf(os.name == "nt", "signals")
    def test_ctrl_c_keeps_the_take_and_a_crash_leaves_a_file_scan_takes(self):
        root = self.project("song")

        def start():
            proc = subprocess.Popen([*gout_cmd(), "record", "2s"], cwd=root, env=self.env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertTrue(proc.stdout.readline().startswith("rec"))  # recording from here
            time.sleep(1.2)
            return proc

        proc = start()
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("silent", out)  # the null input
        track = self.dump()["tracks"][0]
        self.assertEqual(track["offset_ms"], 2000)
        self.assertGreater(track["length_ms"], 900)

        proc = start()
        proc.kill()  # gout dies; the capture program loses its pipe and stops
        proc.communicate(timeout=20)
        crashed = root / "master" / "rec-2.wav"
        self.assertTrue(crashed.exists())
        self.assertGreater(duration(crashed), 0.4)  # the header was brought up to date as it went
        self.assertIn("rec-2", self.gout("scan").stdout)

    def test_inputs_lists_and_picks_for_this_computer(self):
        out = self.gout("inputs").stdout
        self.assertIn("silence", out)
        self.assertIn("system default", out)
        settings = self.tmp / "config" / "gout" / "recording.json"
        self.assertIn("picked for this computer", self.gout("in", "1").stdout)
        self.assertEqual(json.loads(settings.read_text()), {"input": "null"})
        self.gout("inputs", "default")
        self.assertEqual(json.loads(settings.read_text()), {})
        self.assertIn("no input matches", self.gout("inputs", "7", ok=False).stderr)


class RecordAlongTest(GoutTest):
    def tracks(self) -> dict:
        return {t["name"]: t for t in self.dump()["tracks"]}

    def test_a_take_lines_up_with_what_played_however_late_the_output_starts(self):
        root = self.project("song", "click.wav")  # the click at 2.000 s
        self.recorder = "loopback:960:1"  # the input hears the output 20 ms later; the output starts a block late
        out = self.gout("record", "-t", "3s").stdout  # nothing rendered: the project plays live
        self.assertIn("playing from there", out)
        self.assertIn("not calibrated", out)
        self.gout("set", "head", "500ms")
        self.gout("mix")
        self.recorder = "loopback:960"  # the output on time now; master.wav plays from 1 s (0.5 s of head)
        self.gout("record", "1s", "-t", "2.5s", "-n", "late")
        rec, late = self.tracks()["rec"], self.tracks()["late"]
        self.assertEqual((rec["offset_ms"], rec["in_ms"]), (-85, 85))  # two blocks of buffers taken off
        self.assertEqual((late["offset_ms"], late["in_ms"]), (957, 43))  # one block
        self.gout("stems")
        for name in ("02-rec.wav", "03-late.wav"):  # the 20 ms outside the buffers is what calibration is for
            self.assertAlmostEqual(peak_time(root / "stems" / name), 2.020, delta=0.001)
        self.gout("undo")
        self.gout("undo")
        self.assertFalse((root / "master" / "late.wav").exists())
        out = self.gout("record", "-d", "-t", "0.3s", "-n", "dry").stdout
        self.assertIn("not playing", out)
        self.assertEqual((self.tracks()["dry"]["offset_ms"], self.tracks()["dry"]["in_ms"]), (0, 0))

    def test_calibrating_puts_takes_on_time(self):
        root = self.project("song", "click.wav")
        self.recorder = "loopback:960:1"
        out = self.gout("record", "calibrate").stdout
        self.assertIn("20.0 ms outside the buffers (10 of 10 clicks, within 0.0 ms)", out)
        table = json.loads((self.tmp / "config" / "gout" / "recording.json").read_text())["calibration"]
        self.assertEqual(table["loopback -> default @ 48000 Hz / 2048"]["samples"], 960)
        self.assertIn("calibrated with default (48000 Hz): 20.0 ms", self.gout("inputs").stdout)
        self.assertIn("(calibrated)", self.gout("record", "-t", "3s").stdout)
        self.gout("stems")
        self.assertAlmostEqual(peak_time(root / "stems" / "02-rec.wav"), 2.0, delta=0.0006)  # to the ms the timeline has
        self.cwd = self.tmp  # no project needed; the short name
        self.assertIn("it was 20.0 ms", self.gout("rec", "calibrate").stdout)
        self.recorder = "null"
        self.assertIn("nothing came back", self.gout("record", "calibrate", ok=False).stderr)
        self.recorder = "loopback:960"
        self.more_env["GOUT_PORTAUDIO"] = "none"
        self.assertIn("needs PortAudio", self.gout("record", "calibrate", ok=False).stderr)

    def test_without_portaudio_or_anything_to_play_it_records_without_playing(self):
        self.project("song")
        self.recorder = "loopback:960"
        out = self.gout("record", "-t", "0.3s").stdout
        self.assertIn("nothing to play", out)
        self.assertIn("not playing", out)
        self.gout("add", str(self.fx / "click.wav"))
        self.more_env["GOUT_PORTAUDIO"] = "none"
        out = self.gout("record", "-t", "0.3s").stdout
        self.assertIn("without PortAudio", out)
        self.assertIn("not playing", out)
        self.assertEqual({(t["offset_ms"], t["in_ms"]) for n, t in self.tracks().items() if n != "click"}, {(0, 0)})
        self.assertIn("PortAudio: not found", self.gout("version").stdout)
        self.assertIn("no PortAudio", self.gout("inputs").stdout)

    @unittest.skipIf(os.name == "nt", "signals")
    def test_ctrl_c_and_a_crash_while_recording_along(self):
        root = self.project("song", "tone.wav")
        self.recorder = "loopback:0"

        def start():
            proc = subprocess.Popen([*gout_cmd(), "record"], cwd=root, env=self.env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertIn("playing from there", proc.stdout.readline())
            time.sleep(1.2)
            return proc

        proc = start()
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("in time with what played", out)
        self.assertGreater(self.tracks()["rec"]["length_ms"], 900)

        proc = start()
        proc.kill()  # gout dies; the engine sees its stdin close and finishes the file
        proc.communicate(timeout=30)
        crashed = root / "master" / "rec-2.wav"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not crashed.exists() or duration(crashed) < 0.4):
            time.sleep(0.2)
        self.assertGreater(duration(crashed), 0.4)
        self.assertIn("rec-2", self.gout("scan").stdout)


PW_DUMP = [
    {"type": "PipeWire:Interface:Metadata", "metadata": [
        {"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON", "value": {"name": "usb_in"}}]},
    {"type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Sink", "node.name": "speakers", "node.description": "Speakers",
        "audio.channels": 2, "audio.position": "[ FL, FR ]"}}},
    {"type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Source", "node.name": "usb_in", "node.description": "USB Interface",
        "audio.channels": 4, "audio.position": "AUX0,AUX1,AUX2,AUX3"}}},
    {"type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Stream/Output/Audio", "node.name": "ffplay"}}},
]

PACTL_SOURCES = """Source #51
\tState: RUNNING
\tName: alsa_output.pci.analog-stereo.monitor
\tDescription: Monitor of Built-in Audio
\tSample Specification: s32le 2ch 48000Hz
\tChannel Map: front-left,front-right
\tMonitor of Sink: alsa_output.pci.analog-stereo
Source #52
\tState: SUSPENDED
\tName: alsa_input.pci.analog-stereo
\tDescription: Built-in Audio
\tSample Specification: s16le 1ch 44100Hz
\tChannel Map: mono
\tMonitor of Sink: n/a
"""

AVFOUNDATION = """[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x7f8] [1] Scarlett 2i2 USB
"""

DSHOW = """[dshow @ 0000020] "Integrated Camera" (video)
[dshow @ 0000020]   Alternative name "@device_pnp_\\\\?\\usb"
[dshow @ 0000020] "Microphone Array (Realtek(R) Audio)" (audio)
[dshow @ 0000020]   Alternative name "@device_cm_{33D9A762}\\wave_{1}"
[dshow @ 0000020] "Line In (Scarlett 2i2 USB)" (audio)
"""

DSHOW_OLD = """[dshow @ 01] DirectShow video devices
[dshow @ 01]  "Integrated Camera"
[dshow @ 01] DirectShow audio devices
[dshow @ 01]  "Microphone (USB)"
[dshow @ 01]     Alternative name "@device_cm_{1}"
"""


class InputListTest(GoutTest):
    def test_the_lists_each_capture_program_gives(self):
        rec = gout_attr("recorder", "pipewire_inputs")
        usb, monitor = rec(json.dumps(PW_DUMP))
        self.assertEqual((usb.name, usb.channels, usb.positions, usb.default), ("usb_in", 4, "AUX0,AUX1,AUX2,AUX3", True))
        self.assertEqual((monitor.name, monitor.label, monitor.positions, monitor.monitor),
                         ("speakers.monitor", "what Speakers plays", "FL,FR", True))
        self.assertEqual(rec("not json"), [])
        info = "Server Name: PulseAudio\nDefault Source: alsa_input.pci.analog-stereo\n"
        mic, loop = gout_attr("recorder", "pulse_inputs")(PACTL_SOURCES, info)
        self.assertEqual((mic.name, mic.channels, mic.positions, mic.default, mic.monitor),
                         ("alsa_input.pci.analog-stereo", 1, "mono", True, False))
        self.assertEqual((loop.label, loop.monitor), ("what Built-in Audio plays", True))
        alsa = gout_attr("recorder", "alsa_inputs")(
            "null\n    Discard all samples\nlavrate\n    Rate Converter\ndefault\n    Default ALSA Output\n"
            "hw:CARD=PCH,DEV=0\n    HDA Intel PCH, ALC298 Analog\n    Direct hardware device\n")
        self.assertEqual([(i.name, i.default) for i in alsa], [("default", True), ("hw:CARD=PCH,DEV=0", False)])
        mac = gout_attr("recorder", "avfoundation_inputs")(AVFOUNDATION)
        self.assertEqual([i.name for i in mac], ["default", "MacBook Pro Microphone", "Scarlett 2i2 USB"])
        win = gout_attr("recorder", "dshow_inputs")(DSHOW)
        self.assertEqual([(i.name, i.default) for i in win],
                         [("Microphone Array (Realtek(R) Audio)", True), ("Line In (Scarlett 2i2 USB)", False)])
        self.assertEqual([i.name for i in gout_attr("recorder", "dshow_inputs")(DSHOW_OLD)], ["Microphone (USB)"])

    def test_the_capture_command_asks_for_every_channel_and_keeps_some(self):
        Input = gout_attr("recorder", "Input")
        plan = gout_attr("recorder", "capture_plan")
        usb = Input("usb_in", "USB Interface", 4, "AUX0,AUX1,AUX2,AUX3")
        cmd, stream, keep = plan("pw-record", usb, 48000, 3, 2)
        self.assertEqual((stream, keep), (4, [2, 3]))
        self.assertEqual(cmd[cmd.index("--channel-map") + 1], "AUX0,AUX1,AUX2,AUX3")
        self.assertNotIn("-P", cmd)
        monitor = Input("speakers.monitor", "what Speakers plays", 2, "FL,FR", monitor=True)
        cmd, stream, keep = plan("pw-record", monitor, 44100, 2, 1)
        self.assertEqual((cmd[cmd.index("--target") + 1], stream, keep), ("speakers", 2, [1]))
        self.assertIn("{ stream.capture.sink=true }", cmd)
        cmd, _, _ = plan("parecord", monitor, 48000, 1, 1)
        self.assertIn("--device=speakers.monitor", cmd)
        self.assertIn("--latency-msec=20", cmd)
        cmd, stream, keep = plan("dshow", Input("Line In (Scarlett 2i2 USB)", "Line In"), 48000, 2, 1)
        self.assertEqual((stream, keep), (1, [0]))
        self.assertIn("audio=Line In (Scarlett 2i2 USB)", cmd)
        self.assertEqual(cmd[cmd.index("-af") + 1], "pan=mono|c0=c1")
        with self.assertRaises(gout_attr("core", "GoutError")):
            plan("pw-record", monitor, 48000, 2, 2)

    def test_finding_an_input_and_a_picked_one_that_went_away(self):
        Input = gout_attr("recorder", "Input")
        find, current = gout_attr("recorder", "find_input"), gout_attr("recorder", "current_input")
        inputs = [Input("mic", "Built-in Microphone", default=True), Input("usb", "USB Interface"),
                  Input("usb.monitor", "what USB Interface plays", monitor=True)]
        self.assertEqual(find(inputs, "2").name, "usb")
        self.assertEqual(find(inputs, "usb").name, "usb")  # an exact name before a piece of a label
        self.assertEqual(find(inputs, "micro").name, "mic")
        with self.assertRaises(gout_attr("core", "GoutError")):
            find(inputs, "interface")  # two match
        self.assertEqual(current(inputs), (inputs[0], ""))
        gout_attr("recorder", "save_settings")(input="gone")
        chosen, note = current(inputs)
        self.assertEqual(chosen.name, "mic")
        self.assertIn("not here (gone)", note)

    def test_picking_the_portaudio_device_for_an_input(self):
        Device, pick = gout_attr("portaudio", "Device"), gout_attr("engine", "pick_input")

        class Lib:
            def Pa_GetDefaultInputDevice(self):
                return 1

        found = [Device(0, "Speakers (Realtek(R) Audio)", "MME", 0, 2, 48000, 0.1, 0.1),
                 Device(1, "Microphone Array (Realtek(R) ", "MME", 2, 0, 44100, 0.1, 0.1),
                 Device(2, "Microphone Array (Realtek(R) Audio)", "Windows DirectSound", 2, 0, 44100, 0.1, 0.1),
                 Device(3, "Microphone Array (Realtek(R) Audio)", "Windows WASAPI", 2, 0, 48000, 0.1, 0.1),
                 Device(4, "Line In (Scarlett 2i2 USB)", "MME", 2, 0, 48000, 0.1, 0.1)]
        self.assertEqual(pick(Lib(), found, "Microphone Array (Realtek(R) Audio)").index, 3)  # WASAPI first
        self.assertEqual(pick(Lib(), found[:2], "Microphone Array (Realtek(R) Audio)").index, 1)  # MME's 31 characters
        self.assertEqual(pick(Lib(), found, "Line In (Scarlett 2i2 USB)").index, 4)
        self.assertEqual(pick(Lib(), found, "default").index, 1)
        self.assertEqual(pick(Lib(), found, "gone").index, 1)
        os.environ["GOUT_PORTAUDIO"] = "none"
        try:
            self.assertEqual(gout_attr("portaudio", "library_candidates")(), [])
        finally:
            del os.environ["GOUT_PORTAUDIO"]

    def test_finding_the_clicks_in_a_take(self):
        from array import array
        import random
        offsets_of, times = gout_attr("engine", "click_offsets"), gout_attr("engine", "CLICK_TIMES")
        shape = gout_attr("engine", "click_shape")()
        rate, lag = 48000, 1234
        rng = random.Random(1)
        take = array("f", [rng.uniform(-0.002, 0.002) for _ in range(int((times[-1] + 1) * rate))])
        for k, t in enumerate(times):
            if k == 3:
                continue  # one click lost
            at = round(t * rate) + lag
            for i, v in enumerate(shape):
                take[at + i] += 0.3 * v  # quieter than played: a microphone
        offsets, above = offsets_of(take, rate, times)
        self.assertEqual(len(offsets), len(times) - 1)
        self.assertLessEqual(max(abs(o - lag) for o in offsets), 1)  # noise moves the half-level crossing a sample
        self.assertGreater(above, 50)

    def test_the_meter(self):
        meter = gout_attr("recorder", "meter")
        self.assertEqual(meter(0.0, 10), "·········· " + "  -inf dB")
        self.assertTrue(meter(0.1, 10).startswith("███████···"))  # -20 dB of 60
        self.assertIn("CLIP", meter(1.0, 10))


MONITOR_SINKS = """Sink #51
	State: RUNNING
	Name: alsa_output.pci-0000_00_1f.3.analog-stereo
	Description: Built-in Audio Analog Stereo
	Mute: no
	Ports:
		analog-output-speaker: Speakers (type: Speaker, priority: 10000, availability group: Legacy 4, not available)
		analog-output-headphones: Headphones (type: Headphones, priority: 9900, availability group: Legacy 5, available)
	Active Port: analog-output-headphones
Sink #60
	State: SUSPENDED
	Name: laptop_speakers
	Mute: no
	Ports:
		analog-output-speaker: Speakers (type: Speaker, priority: 10000, availability group: Legacy 4, available)
	Active Port: analog-output-speaker
"""

MUTED_SOURCES = """Source #52
	State: RUNNING
	Name: alsa_input.pci-0000_00_1f.3.analog-stereo
	Description: Built-in Audio Analog Stereo
	Mute: yes
	Active Port: analog-input-mic
Source #53
	Name: usb_interface
	Mute: no
"""


class MonitorTest(GoutTest):
    def fake_monitor(self):
        """A stand-in for pw-loopback: writes its pid and arguments, then waits to be stopped."""
        script = self.tmp / "fake-loopback"
        log = self.tmp / "monitor.log"
        script.write_text(f"#!/bin/sh\necho $$ \"$@\" > {log}\nexec sleep 30\n")
        script.chmod(0o755)
        self.more_env["GOUT_MONITOR"] = f"cmd:{script}"
        return log

    def test_speakers_and_mute_are_read_from_pactl(self):
        speaker_output, muted_input = gout_attr("recorder", "speaker_output"), gout_attr("recorder", "muted_input")
        self.assertFalse(speaker_output(MONITOR_SINKS, "alsa_output.pci-0000_00_1f.3.analog-stereo"))  # headphones in
        self.assertTrue(speaker_output(MONITOR_SINKS, "laptop_speakers"))
        self.assertFalse(speaker_output(MONITOR_SINKS, "no_such_sink"))
        self.assertTrue(muted_input(MUTED_SOURCES, "alsa_input.pci-0000_00_1f.3.analog-stereo"))
        self.assertFalse(muted_input(MUTED_SOURCES, "usb_interface"))

    def test_what_an_output_plays_is_never_played_back(self):
        Input, monitor_plan = gout_attr("recorder", "Input"), gout_attr("recorder", "monitor_plan")
        os.environ["GOUT_MONITOR"] = "cmd:/bin/true"
        self.addCleanup(os.environ.pop, "GOUT_MONITOR", None)
        cmd, _ = monitor_plan("pw-record", Input("mic", "Mic", 2), "headphones")
        self.assertEqual(cmd[1:], ["-n", "gout-monitor", "-C", "mic", "-P", "headphones", "-l", "10"])
        cmd, why = monitor_plan("pw-record", Input("out.monitor", "what out plays", 2, monitor=True), "out")
        self.assertIsNone(cmd)
        self.assertIn("echo", why)

    def test_a_take_is_heard_while_it_records_unless_told_not_to(self):
        root = self.project("song")
        self.recorder = f"file:{self.fx / 'click.wav'}"
        log = self.fake_monitor()
        out = self.gout("record", "-t", "1s").stdout
        self.assertIn("you hear the input", out)
        pid, *words = log.read_text().split()
        self.assertEqual(words[words.index("-l") + 1], "10")
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):  # stopped with the take
            os.kill(int(pid), 0)
        log.unlink()
        out = self.gout("record", "-t", "1s", "-M").stdout
        self.assertNotIn("you hear the input", out)
        self.assertFalse(log.exists())
        self.assertEqual(len(self.dump()["tracks"]), 2)

    def test_a_level_check_keeps_nothing_and_says_what_to_do(self):
        root = self.project("song")
        self.recorder = f"file:{self.fx / 'click.wav'}"  # an impulse near full scale
        self.more_env["GOUT_MONITOR"] = "none"
        out = self.gout("record", "check", "-t", "3s").stdout
        self.assertIn("nothing is kept", out)
        self.assertRegex(out, r"check (loudest|it clipped)")
        self.assertEqual(self.dump()["tracks"], [])
        self.assertFalse(any((root / "master").iterdir()))
        self.assertFalse((root / ".gout" / "check.part.wav").exists())
        advice = gout_attr("commands", "level_advice")
        self.assertIn("a good level", advice(10 ** (-10 / 20), 0))
        self.assertIn("about 18 dB up", advice(10 ** (-30 / 20), 0))
        self.assertIn("clipped 2 times", advice(1.0, 2))
        self.assertIn("nothing came in", advice(0.0, 0))
