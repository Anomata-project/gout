import cmath

from helpers import GoutTest, gout_attr


class FormulaTest(GoutTest):
    def setUp(self):
        super().setUp()
        self.parse = gout_attr("formula", "parse")
        self.FormulaError = gout_attr("formula", "FormulaError")

    def test_values_and_exact_slopes(self):
        for text, f in (("z^3 + 7", lambda z: z ** 3 + 7), ("z³ + 7", lambda z: z ** 3 + 7),
                        ("z^8 + 15z^4 - 16", lambda z: z ** 8 + 15 * z ** 4 - 16),
                        ("2z(z+1) - 3i", lambda z: 2 * z * (z + 1) - 3j), ("(z-1)(z+1)^2", lambda z: (z - 1) * (z + 1) ** 2),
                        ("sin(z)", cmath.sin), ("exp(z) - 2", lambda z: cmath.exp(z) - 2),
                        ("2sin(z)cos(z)", lambda z: 2 * cmath.sin(z) * cmath.cos(z)), ("z^z", lambda z: z ** z),
                        ("−z² · 3 + 0.5i", lambda z: -z ** 2 * 3 + 0.5j), ("sqrt(z) - 1", lambda z: cmath.sqrt(z) - 1),
                        ("e^z - pi", lambda z: cmath.exp(z) - cmath.pi), ("tan(z)/z", lambda z: cmath.tan(z) / z)):
            formula = self.parse(text)
            for z in (complex(0.7, -0.4), complex(-1.3, 0.9)):
                self.assertAlmostEqual(formula.value(z), f(z), places=9, msg=text)
                h = 1e-6
                numeric = (f(z + h) - f(z - h)) / (2 * h)
                self.assertLess(abs(formula.slope(z) - numeric), 1e-6, text)
        self.assertEqual(self.parse("z^8 + 15z^4 - 16").pretty, "z⁸ + 15z⁴ - 16")

    def test_refuses_anything_but_a_formula(self):
        for text in ("", "x^2", "import os", "__import__('os')", "z.real", "open(z)", "z^", "7", "z^100",
                     "(z", "sin z", "z;1", "lambda z: 1", "1e999z", "z" * 300, "(" * 80 + "z" + ")" * 80):
            with self.assertRaises(self.FormulaError, msg=text):
                self.parse(text)

    def test_newton_settles_on_the_roots(self):
        for text, roots in (("z^3 + 7", [7 ** (1 / 3) * cmath.exp(1j * cmath.pi * (1 + 2 * k) / 3) for k in range(3)]),
                            ("sin(z)", [0, cmath.pi, -cmath.pi]), ("(z-1)(z+2)", [1, -2])):
            formula = self.parse(text)
            points = [complex(x / 2, y / 2) for x in range(-6, 7) for y in range(-6, 7)]
            steps, finals = formula.newton()(points, 40, 1.0, 1e-10)
            settled = [z for z in finals if z is not None]
            self.assertGreater(len(settled), len(points) * 0.8, text)
            self.assertTrue(all(any(abs(z - r) < 1e-6 for r in roots) or abs(z.real) > 3 for z in settled), text)
