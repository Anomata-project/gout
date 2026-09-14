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

    def test_the_meter(self):
        meter = gout_attr("recorder", "meter")
        self.assertEqual(meter(0.0, 10), "·········· " + "  -inf dB")
        self.assertTrue(meter(0.1, 10).startswith("███████···"))  # -20 dB of 60
        self.assertIn("CLIP", meter(1.0, 10))
