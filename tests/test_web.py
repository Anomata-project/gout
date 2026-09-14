import http.client
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from helpers import GoutTest, duration, gout_attr, gout_cmd, stream_info


class WebTest(GoutTest):
    """A real `gout web` on a free port, with its own root, per test."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "web"
        self.log = open(self.tmp / "server.log", "w")
        self.server = subprocess.Popen([*gout_cmd(), "web", "--port", "0", "--root", str(self.root), "-q"],
                                       env=self.env(), stdout=subprocess.PIPE, stderr=self.log, text=True)
        line = self.server.stdout.readline()
        found = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
        if not found:
            self.fail(f"the server did not start: {line!r} {(self.tmp / 'server.log').read_text()}")
        self.port = int(found.group(1))

    def tearDown(self):
        self.server.terminate()
        self.server.wait(10)
        self.server.stdout.close()
        self.log.close()
        super().tearDown()

    def call(self, method, name, body=None, key=None, data=None, expect=None):
        headers = {}
        if key:
            headers["X-Gout-Key"] = key
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/{name}", data=data, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as reply:
                status, head, raw = reply.status, reply.headers, reply.read()
        except urllib.error.HTTPError as exc:
            status, head, raw = exc.code, exc.headers, exc.read()
        if expect is not None:
            self.assertEqual(status, expect, raw[:500])
        return status, head, raw

    def api(self, method, name, body=None, key=None, data=None, expect=None):
        status, _, raw = self.call(method, "api/" + name, body, key, data, expect)
        return status, json.loads(raw)

    def new_project(self):
        _, reply = self.api("POST", "project", {}, expect=201)
        self.assertRegex(reply["key"], r"^[A-Za-z0-9_-]{43}$")
        return reply["key"]

    def upload(self, key, path, name=None, expect=201):
        query = urllib.parse.urlencode({"name": name or path.name, "width": 100})
        return self.api("PUT", f"files?{query}", key=key, data=path.read_bytes(), expect=expect)[1]

    def run_line(self, key, line):
        return self.api("POST", "run", {"line": line, "width": 100}, key=key, expect=200)[1]

    def folder(self):
        (only,) = [p for p in self.root.iterdir() if p.is_dir()]
        return only

    def test_uploads_become_tracks_and_commands_stay_inside_the_project(self):
        key = self.new_project()
        self.upload(key, self.fx / "tone.wav")
        reply = self.upload(key, self.fx / "click.mp3", name="../../my click .mp3")
        self.assertEqual([t["name"] for t in reply["state"]["tracks"]], ["tone", "my-click"])
        master = self.folder() / "master"
        self.assertEqual(sorted(f.name for f in master.iterdir()), ["my-click.mp3", "tone.wav"])
        self.assertFalse(list(self.tmp.glob("**/my click*")))

        fake = self.tmp / "fake.mp3"
        fake.write_text("not audio")
        self.assertIn("not audio", self.upload(key, fake, expect=415)["error"])
        self.assertIn("not mp3 or wav", self.upload(key, self.fx / "vox.flac", expect=415)["error"])
        disguised = self.tmp / "disguised.wav"
        disguised.write_bytes((self.fx / "click.mp3").read_bytes())
        self.assertIn("holds mp3", self.upload(key, disguised, expect=415)["error"])
        self.assertEqual(len(list(master.iterdir())), 2)
        self.assertFalse(list((self.folder() / ".gout" / "upload").iterdir()))

        reply = self.run_line(key, "gain tone -6")
        self.assertTrue(reply["ok"], reply)
        self.assertIn("-6.0dB", " ".join(row[0] for row in reply["state"]["timeline"]["rows"]))
        reply = self.run_line(key, "e 1 hp80")
        self.assertEqual(reply["state"]["panel"]["who"], "1 tone")
        self.assertEqual(reply["state"]["panel"]["kind"], "eq")
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        for line, says in (("add /etc/passwd", "upload"), ("scan", "upload"), ("ls -p /", "-p"),
                           ("gain 1 -3 -v", "-v"), ("saveas copy", "download"), ("stems", "download"),
                           ("import /etc/hosts", "download"), ("new other", "download"), ("cut x.mp3", "download"),
                           ("set autorender on", "fixed"), ("set bits 24", "fixed"), ("mix", "space plays"),
                           ("rm 'unclosed", "error"), ("frobnicate", "unknown command")):
            reply = self.run_line(key, line)
            self.assertFalse(reply["ok"], line)
            self.assertIn(says, reply["lines"][0], line)
        after = sorted(str(p) for p in self.tmp.rglob("*") if "server.log" not in str(p))
        self.assertEqual([p for p in after if p not in before], [])

        reply = self.run_line(key, "rm 2")  # rm always deletes the file in the preview
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(sorted(f.name for f in master.iterdir()), ["tone.wav"])
        self.assertNotIn(str(self.root), json.dumps(reply))

        self.upload(key, self.fx / "bass.wav")
        reply = self.run_line(key, "undo")  # undoing an upload takes its file away again
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(sorted(f.name for f in master.iterdir()), ["tone.wav"])

    def test_five_files_of_twenty_megabytes(self):
        limits = gout_attr("web", "MAX_FILES"), gout_attr("web", "MAX_FILE_BYTES")
        self.assertEqual(limits, (5, 20_000_000))
        key = self.new_project()
        for i in range(5):
            self.upload(key, self.fx / "click.mp3", name=f"click{i}.mp3")
        reply = self.upload(key, self.fx / "tone.wav", expect=409)
        self.assertIn("5 files", reply["error"])

        other = self.new_project()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        began = time.monotonic()
        connection.putrequest("PUT", "/api/files?name=big.wav")  # the size alone is enough to say no
        connection.putheader("X-Gout-Key", other)
        connection.putheader("Content-Length", "20000001")
        connection.endheaders()
        reply = connection.getresponse()
        self.assertEqual(reply.status, 413)
        self.assertIn("20 MB", json.loads(reply.read())["error"])
        self.assertLess(time.monotonic() - began, 5)
        connection.close()

    def test_the_mix_is_an_mp3_rendered_once_and_removing_a_file_removes_it_from_the_mix(self):
        key = self.new_project()
        status, _ = self.api("GET", "mix.mp3", key=key)
        self.assertEqual(status, 409)
        self.upload(key, self.fx / "tone.wav")      # 6 s
        self.upload(key, self.fx / "click.wav")     # 4 s
        status, head, raw = self.call("GET", "api/mix.mp3", key=key, expect=200)
        self.assertEqual(head["Content-Type"], "audio/mpeg")
        self.assertEqual(head["X-Gout-Head-Ms"], "0")
        mp3 = self.tmp / "mix.mp3"
        mp3.write_bytes(raw)
        self.assertEqual(stream_info(mp3)["codec_name"], "mp3")
        self.assertAlmostEqual(duration(mp3), 6.0, delta=0.1)
        rendered = self.folder() / "master.mp3"
        stamp = rendered.stat().st_mtime_ns
        self.call("GET", "api/mix.mp3", key=key, expect=200)
        self.assertEqual(rendered.stat().st_mtime_ns, stamp)  # current: served, not rendered again
        _, head, _ = self.call("GET", "api/mix.mp3?download=1", key=key, expect=200)
        self.assertIn("attachment", head["Content-Disposition"])
        self.assertTrue(self.api("POST", "state", {"width": 90}, key=key, expect=200)[1]["state"]["mix"]["current"])

        _, reply = self.api("GET", "analysis", key=key, expect=200)
        self.assertEqual(reply["frame_ms"], 25)
        self.assertAlmostEqual(len(reply["bands"]["low"]) * 3 / 4 * 25, 6000, delta=150)  # base64 of a byte a frame

        _, reply = self.api("DELETE", "files/tone.wav", {"width": 90}, key=key, expect=200)
        self.assertEqual([f["name"] for f in reply["state"]["files"]], ["click.wav"])
        for gone in ("master/tone.wav", "master.wav", "master.mp3"):
            self.assertFalse((self.folder() / gone).exists(), gone)
        self.assertFalse(reply["state"]["mix"]["current"])
        status, _, raw = self.call("GET", "api/mix.mp3", key=key, expect=200)
        mp3.write_bytes(raw)
        self.assertAlmostEqual(duration(mp3), 4.0, delta=0.1)
        self.assertEqual(self.api("DELETE", "files/nothing.wav", {}, key=key)[0], 404)

        self.run_line(key, "move 1 10:30")
        status, reply = self.api("GET", "mix.mp3", key=key)
        self.assertEqual(status, 409)
        self.assertIn("up to 10 minutes", reply["error"])

    def test_the_project_zip_opens_in_gout_with_its_defaults_back(self):
        key = self.new_project()
        self.upload(key, self.fx / "tone.wav")
        self.run_line(key, "comp 1 gentle")
        self.call("GET", "api/mix.mp3", key=key, expect=200)
        _, head, raw = self.call("GET", "api/project.zip", key=key, expect=200)
        self.assertEqual(head["Content-Type"], "application/zip")
        path = self.tmp / "preview.zip"
        path.write_bytes(raw)
        with zipfile.ZipFile(path) as z:
            names = sorted(z.namelist())
            z.extractall(self.tmp / "unzipped")
        self.assertEqual(names, ["gout-preview/README.txt", "gout-preview/gout.db", "gout-preview/gout.json",
                                 "gout-preview/master/", "gout-preview/master/tone.wav"])
        project = self.tmp / "unzipped" / "gout-preview"
        self.assertIn("fx comp -18 2:1", self.gout("-p", str(project), "ls").stdout)
        settings = self.gout("-p", str(project), "set").stdout
        self.assertRegex(settings, r"autorender\s+idle")
        self.assertRegex(settings, r"bits\s+32f")
        self.assertIn('"autorender": "idle"', (project / "gout.json").read_text())
        self.gout("-p", str(project), "gain", "1", "-3")
        self.gout("-p", str(project), "undo")
        self.assertRegex(self.gout("-p", str(project), "set").stdout, r"autorender\s+idle")
        self.assertFalse((self.folder() / ".gout" / "export").exists())

    def test_keys_are_not_on_disk_and_a_deleted_project_is_gone(self):
        self.assertEqual(self.api("POST", "state", {})[0], 401)
        self.assertTrue(self.api("POST", "state", {}, key="x" * 43)[1]["gone"])
        self.assertEqual(self.api("POST", "state", {}, key="../" * 15)[0], 401)
        key = self.new_project()
        self.upload(key, self.fx / "tone.wav")
        folder = self.folder()
        for path in folder.rglob("*"):
            if path.is_file():
                self.assertNotIn(key.encode(), path.read_bytes(), path)
        self.assertEqual(self.api("DELETE", "project", {}, key=key, expect=200)[1]["ok"], True)
        self.assertFalse(folder.exists())
        self.assertEqual(self.api("POST", "state", {}, key=key)[0], 401)

    def test_the_cheat_sheet_and_formulas_need_no_project(self):
        _, reply = self.api("GET", "cheat?width=140", expect=200)
        text = "\n".join(reply["lines"])
        for present in ("FRACTAL", "ctrl-space", "undo", "upload mp3 or wav", "eq"):
            self.assertIn(present, text)
        for absent in ("saveas", "stems", "rebuild", "autorender", "gout web"):
            self.assertNotIn(absent, text)
        _, reply = self.api("GET", "formula?text=" + urllib.parse.quote("z³ + 7"), expect=200)
        self.assertEqual(reply["pretty"], "z³ + 7")
        self.assertEqual(reply["program"], {"poly": [[7.0, 0.0], [0.0, 0.0], [0.0, 0.0], [1.0, 0.0]]})
        status, reply = self.api("GET", "formula?text=" + urllib.parse.quote("import os"))
        self.assertEqual(status, 400)
        _, reply = self.api("GET", "info", expect=200)
        self.assertIn("fractal", reply["commands"])
        self.assertNotIn("saveas", reply["commands"])


class StoreTest(GoutTest):
    def test_unused_projects_are_swept_and_new_ones_are_rate_limited(self):
        Store, Refused = gout_attr("web", "Store"), gout_attr("web", "Refused")
        store = Store(self.tmp / "web", days=7, max_projects=4, new_per_hour=2)
        kept, old = store.create("1.2.3.4"), store.create("5.6.7.8")
        store.create("1.2.3.4", now=time.time() + 60)
        with self.assertRaises(Refused) as caught:
            store.create("1.2.3.4", now=time.time() + 120)
        self.assertEqual(caught.exception.status, 429)
        store.create("1.2.3.4", now=time.time() + 3700)  # an hour later it may again
        with self.assertRaises(Refused) as caught:
            store.create("9.9.9.9")
        self.assertEqual(caught.exception.status, 503)

        name = store.folder(old).name
        used = store.folder(old) / ".gout" / "web-used"
        eight_days = time.time() - 8 * 86400
        os.utime(used, (eight_days, eight_days))
        self.assertEqual(store.sweep(), [name])
        self.assertIsNone(store.folder(old))
        self.assertIsNotNone(store.folder(kept))
