"""The web preview's own code: fractal.js against the fractal addon, and the page's files served.

The JavaScript runs in node when node is installed; those tests are skipped without it.
"""
import importlib.util
import json
import shutil
import subprocess
import unittest

from helpers import REPO, GoutTest, gout_attr

NODE = shutil.which("node") or shutil.which("nodejs")
FRACTAL_JS = REPO / "gout" / "webpage" / "fractal.js"
FRACTAL_PY = REPO / "examples" / "addons" / "fractal.py"

NODE_SCRIPT = """
const F = require(process.argv[1]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = [];
for (const c of cases) {
  if (c.kind === "newton") {
    const run = F.newton(c.program);
    const re = Float64Array.from(c.re), im = Float64Array.from(c.im);
    const r = run(re, im, c.limit, c.relax, c.eps);
    out.push({steps: Array.from(r.steps)});
  } else {
    const f = new F.Fractal(null);
    f.choose(c.preset);
    const p = f.preset();
    const run = F.newton(c.program);
    f.programs.set(p.formula, {pretty: "", newton: run});
    out.push({rows: f.frame(null, c.position, c.width, c.height, run), formula: p.formula});
  }
}
process.stdout.write(JSON.stringify(out));
"""


def load_addon():
    spec = importlib.util.spec_from_file_location("gout_test_fractal_web", FRACTAL_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(NODE, "node is not installed")
class FractalScriptTest(GoutTest):
    def node(self, cases):
        result = subprocess.run([NODE, "-e", NODE_SCRIPT, str(FRACTAL_JS)], input=json.dumps(cases),
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_newton_in_the_page_walks_the_points_as_python_does(self):
        parse = gout_attr("formula", "parse")
        formulas = ("z^3 + 7", "z^8 + 15z^4 - 16", "z^5 - z - 1", "sin(z)", "cosh(z) - 2", "tan(z) - 1", "z^z - 2",
                    "sqrt(z) - 1 + i", "exp(z) - z^-2", "(z-1)^(2+i) + 1", "log(z) - 1", "tanh(z)/z - 0.5")
        width, height = 72, 30
        cases, expected = [], []
        for text in formulas:
            formula = parse(text)
            points = [complex(0.13, -0.07) + complex((x - width / 2) * 0.5, height / 2 - y) * (2.2 / (height / 2))
                      for y in range(height) for x in range(width)]
            steps, _ = formula.newton()(list(points), 24, 1.15, 4e-4)
            expected.append(steps)
            cases.append({"kind": "newton", "program": formula.program(), "re": [p.real for p in points],
                          "im": [p.imag for p in points], "limit": 24, "relax": 1.15, "eps": 4e-4})
        for text, want, got in zip(formulas, expected, self.node(cases)):
            same = sum(a == b for a, b in zip(want, got["steps"])) / len(want)
            polynomial = "poly" in parse(text).program()
            self.assertGreaterEqual(same, 0.999 if polynomial else 0.98, f"{text}: {same:.4f} of the points agree")
            self.assertLess(sum(want), 24 * len(want), text)  # something settled, so the comparison means something

    def test_a_frame_in_the_page_is_the_frame_the_addon_draws(self):
        fractal = load_addon()
        parse = gout_attr("formula", "parse")
        ScreenContext = gout_attr("screens", "ScreenContext")
        cases, expected = [], []
        for preset in range(len(fractal.PRESETS)):
            screen = fractal.Fractal()
            screen.load()
            screen.choose(preset, remember=False)
            ctx = ScreenContext(None)
            ctx.position_ms = 12_345.0
            expected.append(screen.frame(ctx, 90, 30))
            cases.append({"kind": "frame", "preset": preset, "position": 12_345.0, "width": 90, "height": 30,
                          "program": parse(fractal.PRESETS[preset]["formula"]).program()})
        for preset, want, got in zip(fractal.PRESETS, expected, self.node(cases)):
            self.assertEqual(got["formula"], preset["formula"])
            cells = [(t, c) for text, classes in want for t, c in zip(text, classes)]
            drawn = [(t, c) for text, classes in got["rows"] for t, c in zip(text, classes)]
            self.assertEqual(len(cells), len(drawn))
            same = sum(a == b for a, b in zip(cells, drawn)) / len(cells)
            self.assertGreaterEqual(same, 0.98, f"{preset['name']}: {same:.4f} of the cells agree")

    def test_the_presets_are_the_addons(self):
        fractal = load_addon()
        result = subprocess.run([NODE, "-e", "process.stdout.write(JSON.stringify(require(process.argv[1]).PRESETS))",
                                 str(FRACTAL_JS)], capture_output=True, text=True, timeout=60)
        page = json.loads(result.stdout)
        for mine, theirs in zip(page, fractal.PRESETS):
            self.assertEqual((mine["name"], mine["formula"], mine["about"], mine.get("zoom")),
                             (theirs["name"], theirs["formula"], theirs["about"], theirs.get("zoom")))
            if "center" in theirs:
                self.assertEqual(complex(*mine["center"]), complex(theirs["center"]))
        self.assertEqual(len(page), len(fractal.PRESETS))


class PageFilesTest(GoutTest):
    def test_the_page_uses_no_inline_script_and_only_its_own_files(self):
        page = REPO / "gout" / "webpage"
        html = (page / "index.html").read_text()
        self.assertNotRegex(html, r"<script>(?!</script>)|<script(?![^>]*\bsrc=)[^>]*>|\son[a-z]+=")
        self.assertNotRegex(html, r"(src|href)=\"(https?:)?//")
        for name in ("app.js", "fractal.js", "style.css"):
            self.assertIn(name, html)
            self.assertTrue((page / name).is_file(), name)
        for script in ("app.js", "fractal.js"):
            text = (page / script).read_text()
            self.assertNotIn("eval(", text)
            self.assertNotIn("new Function", text)
