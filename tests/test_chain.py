import json
import shutil
import sqlite3

from helpers import GoutTest, gout_attr, loudness
from test_ui import FakeScreen


def kinds(track: dict) -> list[str]:
    return [f"{i['kind']} {i['params']}".strip() + ("" if i["on"] else " (off)") for i in track["fx"]]


class ChainTest(GoutTest):
    def test_effects_go_where_they_usually_go_and_can_be_reordered(self):
        self.project("song", "bass.wav")
        self.gout("reverb", "1", "room")
        self.gout("eq", "1", "hp80")
        self.gout("comp", "1", "vocal")
        track = self.dump()["tracks"][0]
        self.assertEqual([i["kind"] for i in track["fx"]], ["eq", "comp", "reverb"])
        self.assertEqual(track["fx"][0], {"kind": "eq", "params": "hp80", "on": True})
        self.gout("fx", "1", "3", "move", "1")
        self.assertEqual([i["kind"] for i in self.dump()["tracks"][0]["fx"]], ["reverb", "eq", "comp"])
        listing = self.gout("fx", "1").stdout
        self.assertIn("1  reverb 0.8s", listing)

    def test_slots_ids_second_instances_and_shortcuts(self):
        root = self.project("song", "bass.wav")
        self.gout("eq", "1", "hp80")
        self.gout("fx", "1", "add", "eq", "+3@1k")
        self.gout("fx", "1", "2", "off")
        self.gout("hp", "1", "120", "24")  # edits the first eq only
        self.assertEqual(kinds(self.dump()["tracks"][0]), ["eq hp120/24", "eq +3@1k (off)"])
        fid = sqlite3.connect(root / "gout.db").execute("SELECT id FROM fx WHERE params = '+3@1k'").fetchone()[0]
        self.gout("fx", "1", f"#{fid}", "-2@500")
        self.assertEqual(kinds(self.dump()["tracks"][0]), ["eq hp120/24", "eq -2@500"])
        self.gout("fx", "1", "9", "on", ok=False)
        self.gout("fx", "1", "add", "nosuch", ok=False)
        self.gout("fx", "1", "1", "rm")
        self.assertEqual(kinds(self.dump()["tracks"][0]), ["eq -2@500"])
        self.gout("fx", "1", "clear")
        self.gout("undo")
        self.assertEqual(kinds(self.dump()["tracks"][0]), ["eq -2@500"])

    def test_order_changes_the_sound(self):
        root = self.project("song", "tone.wav")
        self.gout("eq", "1", "+12@330/2")
        self.gout("comp", "1", "-30", "20:1", "a1", "r50")
        self.gout("mix")
        boost_then_squash = loudness(root / "master.wav")
        self.gout("fx", "1", "2", "move", "1")
        self.gout("mix")
        squash_then_boost = loudness(root / "master.wav")
        self.assertGreater(squash_then_boost - boost_then_squash, 6)

    def test_master_chain_through_set_and_fx(self):
        self.project("song", "tone.wav")
        self.gout("set", "eq", "hp30")
        self.gout("fx", "master", "add", "reverb", "room")
        self.assertEqual(kinds({"fx": self.dump()["master"]["fx"]}), ["eq hp30", "reverb 0.8s p5 d40 w20"])
        self.assertIn("eq hp30 | reverb", self.gout("set").stdout)
        self.assertIn("eq", self.gout("fx", "kinds").stdout)

    def test_missing_effect_kind_is_kept_and_left_out_with_a_warning(self):
        root = self.project("song", "tone.wav")
        self.gout("eq", "1", "hp80")
        db = sqlite3.connect(root / "gout.db")
        db.execute("INSERT INTO fx (owner, pos, kind, params, enabled) VALUES ('tone.wav', 2, 'tremolo', '5hz', 1)")
        db.commit()
        out = self.gout("mix").stdout
        self.assertIn("tremolo is not installed", out)
        self.assertTrue((root / "master.wav").exists())
        self.assertIn("tremolo 5hz (not installed)", self.gout("fx", "1").stdout)
        self.assertEqual(kinds(self.dump()["tracks"][0]), ["eq hp80", "tremolo 5hz"])

    def test_the_sheet_edits_chains(self):
        root = self.project("song", "bass.wav")
        self.gout("eq", "1", "hp80")
        import os
        os.environ["GOUT_ADDONS"] = str(self.addons)
        project = gout_attr("project", "Project")(root)
        ui = gout_attr("tui", "Tui")(project, FakeScreen())

        def type_on(row_id, text):
            ui.sheet_rows = ui.sheet_build()
            ui.sheet_cur = next(i for i, r in enumerate(ui.sheet_rows) if r["id"] == row_id)
            for ch in text:
                ui.handle(ch)

        ui.handle("\x05")
        eq_id = project.tracks()[0]["fx"][0]["id"]
        type_on("t1:+fx", "reverb hall")
        type_on(f"fx#{eq_id}", "off")
        type_on("master:+fx", "comp glue")
        ui.handle("\x13")
        self.assertEqual(ui.sheet_errors, {})
        self.assertEqual(kinds(project.tracks()[0]), ["eq hp80 (off)", "reverb 2.6s p25 d50 w25"])
        reverb_id = project.tracks()[0]["fx"][1]["id"]
        type_on(f"fx#{reverb_id}", "move 1")
        ui.handle("\x13")
        self.assertEqual([i["kind"] for i in project.tracks()[0]["fx"]], ["reverb", "eq"])
        self.assertEqual([i["kind"] for i in project.chain("@master")], ["comp"])
        names = [r["name"] for r in ui.sheet_build() if r["id"] and r["id"].startswith("fx#")]
        self.assertEqual(names, ["1 comp", "1 reverb", "2 eq"])
        type_on(f"fx#{reverb_id}", "bogus")
        ui.handle("\x13")
        self.assertIn(f"fx#{reverb_id}", ui.sheet_errors)

    def test_old_database_migrates_to_chains(self):
        root = self.tmp / "old"
        (root / "master").mkdir(parents=True)
        shutil.copy(self.fx / "bass.wav", root / "master" / "bass.wav")
        db = sqlite3.connect(root / "gout.db")
        db.executescript("""
            CREATE TABLE project (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tracks (n INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, file TEXT NOT NULL,
                kind TEXT NOT NULL, length_ms INTEGER NOT NULL, channels INTEGER NOT NULL,
                sample_rate INTEGER NOT NULL, offset_ms INTEGER NOT NULL DEFAULT 0, in_ms INTEGER NOT NULL DEFAULT 0,
                out_ms INTEGER, gain_db REAL NOT NULL DEFAULT 0, pan REAL NOT NULL DEFAULT 0,
                mute INTEGER NOT NULL DEFAULT 0, solo INTEGER NOT NULL DEFAULT 0,
                eq TEXT NOT NULL DEFAULT '', eq_on INTEGER NOT NULL DEFAULT 1,
                comp TEXT NOT NULL DEFAULT '', comp_on INTEGER NOT NULL DEFAULT 1,
                delay TEXT NOT NULL DEFAULT '', delay_on INTEGER NOT NULL DEFAULT 1,
                reverb TEXT NOT NULL DEFAULT '', reverb_on INTEGER NOT NULL DEFAULT 1);
            INSERT INTO project VALUES ('name', 'old'), ('rate', '48000'), ('autorender', 'off'),
                ('eq', 'ls200:+2 hs6k:-2');
            INSERT INTO tracks (n, name, file, kind, length_ms, channels, sample_rate, offset_ms, eq, reverb, reverb_on)
                VALUES (1, 'bass', 'bass.wav', 'wav', 4000, 2, 44100, 500, 'hp80', '2.6s p25 d50 w25', 0);
        """)
        db.commit()
        db.close()
        self.cwd = root
        doc = self.dump()
        self.assertEqual(kinds(doc["tracks"][0]), ["eq hp80", "reverb 2.6s p25 d50 w25 (off)"])
        self.assertEqual(kinds({"fx": doc["master"]["fx"]}), ["eq ls200:+2 hs6k:-2"])
        self.assertEqual(doc["tracks"][0]["offset_ms"], 500)
        columns = {r[1] for r in sqlite3.connect(root / "gout.db").execute("PRAGMA table_info(tracks)")}
        self.assertNotIn("eq", columns)
        self.assertNotIn("eq", self.gout("set").stdout.split("effects")[0])
        self.gout("mix")

    def test_old_json_imports_as_chains(self):
        self.project("song", "bass.wav")
        legacy = {"project": {"eq": "hp30", "lufs": "-16"},
                  "tracks": [{"file": "bass.wav", "name": "bass", "offset_ms": 250, "eq": "hp80",
                              "comp": "-18 4:1 a20 r200 k6", "comp_on": 0}]}
        path = self.tmp / "legacy.json"
        path.write_text(json.dumps(legacy))
        out = self.gout("import", str(path)).stdout
        self.assertNotIn("ignored", out)
        doc = self.dump()
        self.assertEqual(kinds(doc["tracks"][0]), ["eq hp80", "comp -18 4:1 a20 r200 k6 (off)"])
        self.assertEqual(kinds({"fx": doc["master"]["fx"]}), ["eq hp30"])
        self.assertEqual(doc["project"]["lufs"], "-16")
