import subprocess

from helpers import duration, ffmpeg, gout_attr, GoutTest


def frame_at(path, seconds: float, width: int, height: int) -> bytes:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(seconds), "-i", str(path),
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, check=True).stdout[:width * height * 3]


def pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    i = 3 * (y * width + x)
    return tuple(frame[i:i + 3])


class VideoTest(GoutTest):
    def setUp(self):
        super().setUp()
        self.more_env["GOUT_VIDEO_GRID"] = "40x12"  # 480 by 288 pixels

    def test_a_cover_with_black_bars_and_the_song(self):
        root = self.project("song", "tone.wav")  # 6 s
        cover = self.tmp / "cover.png"
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=200x100:d=1", "-frames:v", "1", str(cover))  # 2:1, wider than the frame
        out = self.gout("video", str(cover)).stdout
        self.assertIn("rendering master.wav first", out)
        video = root / "master.mp4"
        streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,r_frame_rate",
                                  "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(streams), ["aac,0/0", "h264,480,288,25/1"])  # audio has no frame rate
        self.assertAlmostEqual(duration(video), 6.0, delta=0.1)
        frame = frame_at(video, 1.0, 480, 288)
        self.assertLess(max(pixel(frame, 480, 240, 5)), 20)  # the bar above: 480x240 fits, 24 rows each side
        red, green, blue = pixel(frame, 480, 240, 144)
        self.assertGreater(red, 200)
        self.assertLess(max(green, blue), 40)
        self.gout("vd", str(cover), "-o", str(self.tmp / "other.mp4"))
        self.assertTrue((self.tmp / "other.mp4").exists())
        self.assertFalse(any(p.name.endswith(".part.mp4") for p in root.iterdir()))
        self.assertIn("no such image", self.gout("video", "nothing.png", ok=False).stderr)

    def test_characters_are_drawn_in_the_theme_colours(self):
        Atlas, compose, colours = gout_attr("video", "Atlas"), gout_attr("video", "compose"), gout_attr("video", "class_colours")
        atlas = Atlas()
        self.assertTrue(any(any(row) for row in atlas.glyph("@", (255, 255, 255))))
        self.assertIs(atlas.glyph(" ", (255, 255, 255)), atlas.blank)
        palette = colours(None)
        self.assertEqual(palette["1"], (0x87, 0xd7, 0x87))  # the second track colour
        frame = compose([("@", "1"), ("", "")], atlas, palette, 2, 2)
        self.assertEqual(len(frame), 2 * 12 * 2 * 24 * 3)
        lit = [frame[i:i + 3] for i in range(0, len(frame), 3) if any(frame[i:i + 3])]
        brightest = max(lit, key=sum)
        self.assertEqual(tuple(brightest), (0x87, 0xd7, 0x87))  # a fully covered pixel is the colour itself
        self.assertEqual(gout_attr("video", "xterm_rgb")(196), (255, 0, 0))
