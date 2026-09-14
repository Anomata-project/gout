"""Formulas in z: read from text, differentiated exactly, compiled for Newton's method.

    f = parse("z^3 + 7")
    f.value(1j), f.slope(1j)          # w and dw/dz
    steps, finals = f.newton()(points, limit=24, relax=1.0, eps=1e-4)

The text is read by a small parser into a tree of numbers, z, + - * / ^ and a fixed list of
functions; the derivative is worked out on that tree. Python source is generated only from the
tree, never from the text, so a formula in a settings file cannot run code. Written for the
fractal screen, usable by any addon.

Accepted: z; numbers such as 7, 0.5, 2e-3, 3i (i or j is the imaginary unit), pi and e; + - * /
and ^ (or **); implicit multiplication (3z, 2z^2, z(z+1), 2sin(z)); superscript powers (z³); the
functions sin cos tan sinh cosh tanh exp log sqrt.
"""
from __future__ import annotations

import cmath
import math
import re

FUNCTIONS = ("sin", "cos", "tan", "sinh", "cosh", "tanh", "exp", "log", "sqrt")
CONSTANTS = {"pi": math.pi, "e": math.e}
ENV = {name: getattr(cmath, name) for name in FUNCTIONS}
MAX_LENGTH = 200
MAX_POWER = 64
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
TO_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
TOKEN = re.compile(r"\s*(?:(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)(?P<imag>[ij](?![a-z]))?"
                   r"|(?P<name>[a-z]+)|(?P<op>\*\*|[-+*/^()]))")


class FormulaError(ValueError):
    pass


# ---- the tree: tuples ("num", complex) ("z",) ("neg", a) ("add"|"sub"|"mul"|"div"|"pow", a, b) ("call", name, a)

ZERO, ONE = ("num", 0j), ("num", 1 + 0j)


def num(v) -> tuple:
    return ("num", complex(v))


def is_num(node, value=None) -> bool:
    return node[0] == "num" and (value is None or node[1] == value)


def add(a, b):
    if is_num(a, 0):
        return b
    if is_num(b, 0):
        return a
    if is_num(a) and is_num(b):
        return num(a[1] + b[1])
    return ("add", a, b)


def sub(a, b):
    if is_num(b, 0):
        return a
    if is_num(a) and is_num(b):
        return num(a[1] - b[1])
    return ("sub", a, b) if not is_num(a, 0) else neg(b)


def mul(a, b):
    if is_num(a, 0) or is_num(b, 0):
        return ZERO
    if is_num(a, 1):
        return b
    if is_num(b, 1):
        return a
    if is_num(a) and is_num(b):
        return num(a[1] * b[1])
    return ("mul", a, b)


def div(a, b):
    if is_num(b, 1):
        return a
    if is_num(a, 0):
        return ZERO
    return ("div", a, b)


def power(a, b):
    if is_num(b, 1):
        return a
    if is_num(b, 0):
        return ONE
    return ("pow", a, b)


def neg(a):
    if is_num(a):
        return num(-a[1])
    return ("neg", a)


def call(name, a):
    return ("call", name, a)


# ---- reading

def tokens(text: str) -> list[tuple[str, object]]:
    text = text.replace("−", "-").replace("·", "*").replace("×", "*").lower()
    text = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "^" + m.group(0).translate(SUPERSCRIPTS), text)
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"cannot read {text[pos:].strip()[:12]!r} in the formula")
        pos = m.end()
        if m.group("num") is not None:
            value = float(m.group("num"))
            if not math.isfinite(value):
                raise FormulaError(f"{m.group('num')} is too large a number")
            out.append(("num", complex(0, value) if m.group("imag") else complex(value)))
        elif m.group("name"):
            name = m.group("name")
            if name == "z":
                out.append(("z", None))
            elif name in ("i", "j"):
                out.append(("num", 1j))
            elif name in CONSTANTS:
                out.append(("num", complex(CONSTANTS[name])))
            elif name in FUNCTIONS:
                out.append(("func", name))
            else:
                raise FormulaError(f"unknown name {name!r}: use z, i, pi, e and {' '.join(FUNCTIONS)}")
        else:
            out.append(("op", "^" if m.group("op") == "**" else m.group("op")))
    return out


class Parser:
    def __init__(self, text: str):
        self.toks = tokens(text)
        self.pos = 0
        self.depth = 0

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else ("end", None)

    def take(self, kind, value=None):
        tok = self.peek()
        if tok[0] != kind or (value is not None and tok[1] != value):
            want = value or kind
            got = "the end" if tok[0] == "end" else repr(tok[1] if tok[1] is not None else "z")
            raise FormulaError(f"expected {want!r} but found {got} in the formula")
        self.pos += 1
        return tok

    def parse(self):
        node = self.expr()
        if self.peek()[0] != "end":
            raise FormulaError(f"unexpected {self.peek()[1]!r} in the formula")
        return node

    def expr(self):
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take("op")[1]
            node = add(node, self.term()) if op == "+" else sub(node, self.term())
        return node

    def starts_atom(self) -> bool:
        kind, value = self.peek()
        return kind in ("num", "z", "func") or (kind, value) == ("op", "(")

    def term(self):
        node = self.unary()
        while True:
            if self.peek() in (("op", "*"), ("op", "/")):
                op = self.take("op")[1]
                node = mul(node, self.unary()) if op == "*" else div(node, self.unary())
            elif self.starts_atom():  # 3z, z(z+1), 2sin(z)
                node = mul(node, self.power())
            else:
                return node

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take("op")
            return neg(self.unary())
        if self.peek() == ("op", "+"):
            self.take("op")
            return self.unary()
        return self.power()

    def power(self):
        node = self.atom()
        if self.peek() == ("op", "^"):
            self.take("op")
            exponent = self.unary()
            if is_num(exponent) and abs(exponent[1]) > MAX_POWER:
                raise FormulaError(f"powers up to {MAX_POWER}")
            node = power(node, exponent)
        return node

    def atom(self):
        self.depth += 1
        if self.depth > 60:
            raise FormulaError("the formula nests too deep")
        try:
            kind, value = self.peek()
            if kind == "num":
                self.take("num")
                return ("num", value)
            if kind == "z":
                self.take("z")
                return ("z",)
            if kind == "func":
                self.take("func")
                self.take("op", "(")
                inner = self.expr()
                self.take("op", ")")
                return call(value, inner)
            if (kind, value) == ("op", "("):
                self.take("op", "(")
                inner = self.expr()
                self.take("op", ")")
                return inner
            got = "the end" if kind == "end" else repr(value)
            raise FormulaError(f"expected a number, z or ( but found {got} in the formula")
        finally:
            self.depth -= 1


# ---- the derivative

def derivative(node):
    kind = node[0]
    if kind == "num":
        return ZERO
    if kind == "z":
        return ONE
    if kind == "neg":
        return neg(derivative(node[1]))
    if kind in ("add", "sub"):
        a, b = derivative(node[1]), derivative(node[2])
        return add(a, b) if kind == "add" else sub(a, b)
    if kind == "mul":
        a, b = node[1], node[2]
        return add(mul(derivative(a), b), mul(a, derivative(b)))
    if kind == "div":
        a, b = node[1], node[2]
        return div(sub(mul(derivative(a), b), mul(a, derivative(b))), power(b, num(2)))
    if kind == "pow":
        a, b = node[1], node[2]
        if is_num(b):
            return mul(mul(b, power(a, num(b[1] - 1))), derivative(a))
        # a^b = exp(b log a)
        return mul(node, add(mul(derivative(b), call("log", a)), div(mul(b, derivative(a)), a)))
    if kind == "call":
        name, a = node[1], node[2]
        da = derivative(a)
        outer = {
            "sin": lambda: call("cos", a),
            "cos": lambda: neg(call("sin", a)),
            "tan": lambda: div(ONE, power(call("cos", a), num(2))),
            "sinh": lambda: call("cosh", a),
            "cosh": lambda: call("sinh", a),
            "tanh": lambda: div(ONE, power(call("cosh", a), num(2))),
            "exp": lambda: call("exp", a),
            "log": lambda: div(ONE, a),
            "sqrt": lambda: div(ONE, mul(num(2), call("sqrt", a))),
        }[name]()
        return mul(outer, da)
    raise FormulaError(f"cannot differentiate {kind}")


# ---- writing Python

def source(node) -> str:
    kind = node[0]
    if kind == "num":
        v = node[1]
        return repr(v.real) if v.imag == 0 else f"({v!r})"  # (1+2j): a literal, no name needed
    if kind == "z":
        return "z"
    if kind == "neg":
        return f"(-{source(node[1])})"
    if kind == "call":
        return f"{node[1]}({source(node[2])})"
    a, b = source(node[1]), source(node[2])
    if kind == "pow":
        exponent = node[2]
        if is_num(exponent) and exponent[1].imag == 0 and exponent[1].real == int(exponent[1].real):
            b = str(int(exponent[1].real))  # z**3 takes the fast integer power
        return f"({a} ** {b})"
    return f"({a} {dict(add='+', sub='-', mul='*', div='/')[kind]} {b})"


def coefficients(node) -> dict[int, complex] | None:
    """The polynomial as {power: coefficient}, or None when it is not one."""
    kind = node[0]
    if kind == "num":
        return {0: node[1]}
    if kind == "z":
        return {1: 1 + 0j}
    if kind == "neg":
        inner = coefficients(node[1])
        return None if inner is None else {k: -v for k, v in inner.items()}
    if kind in ("add", "sub", "mul"):
        a, b = coefficients(node[1]), coefficients(node[2])
        if a is None or b is None:
            return None
        out: dict[int, complex] = {}
        if kind == "mul":
            for ka, va in a.items():
                for kb, vb in b.items():
                    out[ka + kb] = out.get(ka + kb, 0j) + va * vb
        else:
            sign = 1 if kind == "add" else -1
            out = dict(a)
            for k, v in b.items():
                out[k] = out.get(k, 0j) + sign * v
        return None if max(out, default=0) > MAX_POWER else out
    if kind == "pow" and is_num(node[2]):
        n = node[2][1]
        if n.imag != 0 or n.real != int(n.real) or not 0 <= n.real <= MAX_POWER:
            return None
        base = coefficients(node[1])
        if base is None:
            return None
        out = {0: 1 + 0j}
        for _ in range(int(n.real)):
            out = coefficients(("mul", ("poly", out), ("poly", base)))
        return out
    if kind == "poly":
        return node[1]
    return None


def horner(coeffs: dict[int, complex], indent: str) -> str:
    """Python lines that leave the polynomial in w and its slope in d, by Horner's rule."""
    degree = max(coeffs)
    lit = lambda v: repr(v.real) if v.imag == 0 else f"({v!r})"
    lines = [f"w = {lit(coeffs.get(degree, 0j))}", "d = 0.0"]
    for k in range(degree - 1, -1, -1):
        lines.append("d = d * z + w")
        c = coeffs.get(k, 0j)
        lines.append(f"w = w * z + {lit(c)}" if c != 0 else "w = w * z")
    return "\n".join(indent + line for line in lines)


def uses_z(node) -> bool:
    return node[0] == "z" or any(uses_z(part) for part in node[1:] if isinstance(part, tuple))


NEWTON = """
def newton(zs, limit, relax, eps):
    count = len(zs)
    steps = [limit] * count
    finals = [None] * count
    active = range(count)
    for n in range(limit):
        still = []
        for i in active:
            z = zs[i]
            try:
{body}
                if abs(s) < eps:
                    steps[i] = n
                    finals[i] = z
                    continue
                zs[i] = z - relax * s
            except (ZeroDivisionError, OverflowError, ValueError):
                continue
            still.append(i)
        active = still
        if not active:
            break
    return steps, finals
"""


class Formula:
    def __init__(self, text: str):
        text = " ".join(str(text).split())
        if not text:
            raise FormulaError("the formula is empty")
        if len(text) > MAX_LENGTH:
            raise FormulaError(f"a formula of up to {MAX_LENGTH} characters")
        self.text = text
        self.tree = Parser(text).parse()
        if not uses_z(self.tree):
            raise FormulaError("the formula has no z in it")
        self.slope_tree = derivative(self.tree)
        if not uses_z(self.slope_tree) and is_num(self.slope_tree, 0):
            raise FormulaError("the formula does not change with z")
        self.value = eval(compile(f"lambda z: {source(self.tree)}", "<formula>", "eval"), dict(ENV, __builtins__={}))
        self.slope = eval(compile(f"lambda z: {source(self.slope_tree)}", "<formula>", "eval"),
                          dict(ENV, __builtins__={}))
        self._newton = None

    @property
    def pretty(self) -> str:
        """The text with powers as superscripts: z^3 + 7 reads z³ + 7."""
        text = self.text.replace("**", "^")
        return re.sub(r"\s*\^\s*(\d+)", lambda m: m.group(1).translate(TO_SUPERSCRIPT), text)

    def newton(self):
        """newton(points, limit, relax, eps) -> (steps, finals): Newton's method on every point at
        once, `points` changed in place; steps is limit for a point that never settled (or hit a
        pole), finals the point it settled on (or None)."""
        if self._newton is None:
            scope = dict(ENV, __builtins__={"range": range, "len": len, "abs": abs,
                                            "ZeroDivisionError": ZeroDivisionError,
                                            "OverflowError": OverflowError, "ValueError": ValueError})
            indent = " " * 16
            coeffs = coefficients(self.tree)
            if coeffs is not None and max(coeffs) >= 1:  # a polynomial: Horner's rule, no powers
                body = horner(coeffs, indent) + f"\n{indent}s = w / d"
            else:
                body = f"{indent}s = ({source(self.tree)}) / ({source(self.slope_tree)})"
            exec(compile(NEWTON.format(body=body), "<formula>", "exec"), scope)
            self._newton = scope["newton"]
        return self._newton


def parse(text: str) -> Formula:
    return Formula(text)
