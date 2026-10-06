"""What a tube with side holes resonates at.

Plane waves in the bore, as a chain of transfer matrices: each stretch of bore, each hole and
each end is a 2 x 2 matrix between the pressure and the flow on its two sides. The pieces are the
standard ones of woodwind acoustics:

- wall losses: viscous and thermal (Zwikker and Kosten), from their expansion for a tube much
  wider than its boundary layers, or the exact Bessel ratio where it is not;
- a side hole: a T-section. Series length correction after Lefebvre and Scavone (2012), inner
  and matching corrections after Nederveen et al. (1998) and Dubos et al. (1999); an open hole's
  chimney is a short lossy tube that radiates, a closed one a small volume;
- an open end: the radiation of a thin-walled tube (Norris and Sheng's fits to Levine and Schwinger);
- the end that is blown: an opening smaller than the bore, with its own length, in series with
  the bore. A flute sounds near the frequencies where the impedance the jet meets, that opening
  and the bore together, is smallest.

Checked against OpenWInD's finite elements on tubes of this size: within 7 cents (tests).
Lengths in metres, frequencies in Hz, everything else SI.
"""
from __future__ import annotations

import cmath
import math

EXACT_BELOW = 8.0   # tube radius over boundary layer thickness: under this the expansion is not good enough


class Air:
    """Air at a temperature (Chaigne and Kergomard's formulas, as OpenWInD uses them)."""

    def __init__(self, celsius: float = 20.0):
        kelvin = celsius + 273.15
        self.celsius = celsius
        self.c = 331.45 * math.sqrt(kelvin / 273.15)
        self.rho = 1.2929 * 273.15 / kelvin
        self.mu = 1.708e-5 * (1 + 0.0029 * celsius)
        self.gamma = 1.402
        self.prandtl = self.mu * 240 * 4.184 / (5.77e-3 * (1 + 0.0033 * celsius) * 4.184)
        self.heat = (self.gamma - 1) / math.sqrt(self.prandtl)


def bessel_ratio(z: complex) -> complex:
    """J1(z) / J0(z), by its continued fraction from the tail."""
    if abs(z) < 1e-9:
        return z / 2
    ratio = 0j
    for m in range(int(abs(z)) + 30, 0, -1):
        ratio = 1 / (2 * m / z - ratio)
    return ratio


def duct(air: Air, omega: float, radius: float, length: float) -> tuple:
    """(A, B, C, D) of a length of cylinder with wall losses."""
    area = math.pi * radius * radius
    rv = radius * math.sqrt(omega * air.rho / air.mu)
    if rv >= EXACT_BELOW:
        x = (1 - 1j) / (math.sqrt(2) * rv)
        gamma = 1j * omega / air.c * (1 + x * (1 + air.heat))
        zc = air.rho * air.c / area * (1 + x * (1 - air.heat))
    else:
        kv = cmath.sqrt(-1j * omega * air.rho / air.mu)
        kt = kv * math.sqrt(air.prandtl)
        zv = 1j * omega * air.rho / area / (1 - 2 * bessel_ratio(kv * radius) / (kv * radius))
        yt = 1j * omega * area / (air.rho * air.c ** 2) * (1 + (air.gamma - 1) * 2 * bessel_ratio(kt * radius) / (kt * radius))
        gamma, zc = cmath.sqrt(zv * yt), cmath.sqrt(zv / yt)
    e = cmath.exp(gamma * length)
    ch, sh = (e + 1 / e) / 2, (e - 1 / e) / 2
    return ch, zc * sh, sh / zc, ch


def radiation(air: Air, omega: float, radius: float, flanged: bool = False) -> complex:
    """The impedance an open end radiates into: of a thin-walled tube, or of a hole in a wide
    flat face (a flange), which sits half way to an infinite wall for the mass and radiates
    into half the space."""
    k = omega / air.c
    ka = k * radius
    z0 = air.rho * air.c / (math.pi * radius * radius)
    if flanged:
        return z0 * (ka * ka / 2 + 1j * 0.8216 * ka)
    mod = (1 + 0.2 * ka - 0.084 * ka * ka) / (1 + 0.2 * ka + (0.5 - 0.084) * ka * ka)
    delta = radius * (0.6133 * (1 + 0.044 * ka * ka) / (1 + 0.19 * ka * ka) - 0.02 * math.sin(2 * ka) ** 2)
    refl = -mod * cmath.exp(-2j * k * delta)
    return z0 * (1 + refl) / (1 - refl)


def side_hole(air: Air, omega: float, bore_radius: float, radius: float, chimney: float, is_open: bool) -> tuple:
    """(A, B, C, D) of a side hole: a shunt impedance between two halves of a series one."""
    k = omega / air.c
    d = min(1.0, radius / bore_radius)   # the fits end where the hole is as wide as the bore
    z0h = air.rho * air.c / (math.pi * radius * radius)
    inner = radius * (0.822 - 0.095 * d - 1.566 * d ** 2 + 2.138 * d ** 3 - 1.640 * d ** 4 + 0.502 * d ** 5)
    match = radius * d * (1 + 0.207 * d ** 3) / 8
    if is_open:
        series = radius * d * d * (-0.35 + 0.06 * math.tanh(2.7 * chimney / radius))
        a, b, c, dd = duct(air, omega, radius, chimney + match)
        out = radiation(air, omega, radius)
        shunt = (a * out + b) / (c * out + dd) + 1j * z0h * k * inner
    else:
        series = radius * d * d * (-0.12 - 0.17 * math.tanh(2.4 * chimney / radius))
        shunt = -1j * z0h / math.tan(k * (chimney + match)) + 1j * z0h * k * inner
    za = 1j * z0h * k * series
    half = za / (2 * shunt)
    return 1 + half, za * (1 + za / (4 * shunt)), 1 / shunt, 1 + half


class Mouth:
    """The opening that is blown across: what the lips leave of an end, or a window. radius is
    that of a circle of the same area, length what the air in it has to move through: the wall
    and the lip. flanged: whether a face sits around it."""

    def __init__(self, radius: float, length: float, flanged: bool = True):
        self.radius, self.length, self.flanged = radius, length, flanged


class Resonance:
    """A frequency the pipe favours. q: how sharp it is (the higher, the readier it speaks)."""

    def __init__(self, hz: float, q: float):
        self.hz, self.q = hz, q

    def __repr__(self) -> str:
        return f"Resonance({self.hz:.1f} Hz, q {self.q:.0f})"


class Pipe:
    """A bore as (place, radius) points from the blown end, and holes as (place, radius, chimney)."""

    def __init__(self, profile: list[tuple[float, float]], holes: list[tuple[float, float, float]] = (),
                 celsius: float = 20.0, slice_m: float = 0.002):
        self.profile = sorted(profile)
        self.holes = sorted(holes)
        self.air = Air(celsius)
        self.length = self.profile[-1][0] - self.profile[0][0]
        # the bore in slices, cut at the holes: [(radius, length) ...] for each stretch between holes
        cuts = [self.profile[0][0]] + [h[0] for h in self.holes] + [self.profile[-1][0]]
        straight = len({round(r, 9) for _, r in self.profile}) == 1
        self.stretches: list[list[tuple[float, float]]] = []
        for lo, hi in zip(cuts, cuts[1:]):
            count = 1 if straight else max(1, round((hi - lo) / slice_m))
            step = (hi - lo) / count
            self.stretches.append([(self.radius_at(lo + (i + 0.5) * step), step) for i in range(count)] if hi > lo else [])

    def radius_at(self, x: float) -> float:
        pts = self.profile
        if x <= pts[0][0]:
            return pts[0][1]
        for (x0, r0), (x1, r1) in zip(pts, pts[1:]):
            if x <= x1:
                return r0 + (r1 - r0) * (x - x0) / (x1 - x0) if x1 > x0 else r1
        return pts[-1][1]

    def chain(self, omega: float, opened) -> tuple:
        """The matrix from the blown end to the far end; opened says which holes are open, in order."""
        air = self.air
        a, b, c, d = 1, 0, 0, 1
        for n, stretch in enumerate(self.stretches):
            pieces = [duct(air, omega, radius, length) for radius, length in stretch]
            if n < len(self.holes):
                x, radius, chimney = self.holes[n]
                pieces.append(side_hole(air, omega, self.radius_at(x), radius, chimney, bool(opened[n])))
            for a2, b2, c2, d2 in pieces:
                a, b, c, d = a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2
        return a, b, c, d

    def impedance(self, hz: float, opened=(), far: str = "open") -> complex:
        """The impedance of the bore at the blown end. far: open (it radiates) or closed."""
        omega = 2 * math.pi * hz
        a, b, c, d = self.chain(omega, opened or [False] * len(self.holes))
        if far == "closed":
            return a / c
        load = radiation(self.air, omega, self.profile[-1][1])
        return (a * load + b) / (c * load + d)

    def met_by_the_jet(self, hz: float, opened=(), far: str = "open", mouth: Mouth | None = None) -> complex:
        """What the jet works against: the bore through the mouth opening, and the air outside
        it. With no mouth, the bare bore: an end that is open all the way and ideally so."""
        z = self.impedance(hz, opened, far)
        if mouth is None:
            return z
        omega = 2 * math.pi * hz
        ratio = min(1.0, mouth.radius / self.profile[0][1])
        # the air just inside a small opening moves with it: a length that goes to nothing as the opening fills the bore
        inside = 0.8216 * mouth.radius * max(0.0, 1 - 1.35 * ratio + 0.31 * ratio ** 3)
        a, b, c, d = duct(self.air, omega, mouth.radius, mouth.length + inside)
        return (a * z + b) / (c * z + d) + radiation(self.air, omega, mouth.radius, mouth.flanged)

    def resonances(self, opened=(), far: str = "open", mouth: Mouth | None = None, lo: float = 80.0, hi: float = 6000.0,
                   step: float = 40.0, most: int = 4) -> list[Resonance]:
        """The frequencies between lo and hi where the impedance met by the jet is smallest: where
        its imaginary part crosses zero going up. The lowest `most` of them."""
        found: list[Resonance] = []

        def reactance(f: float) -> float:
            return self.met_by_the_jet(f, opened, far, mouth).imag

        f0, x0 = lo, reactance(lo)
        f1 = lo + step
        while f1 <= hi and len(found) < most:
            x1 = reactance(f1)
            if x0 < 0 <= x1:
                a, b, xa, xb = f0, f1, x0, x1
                for _ in range(30):  # false position with a halving step when one side stalls
                    mid = b - xb * (b - a) / (xb - xa) if xb != xa else (a + b) / 2
                    if not a < mid < b:
                        mid = (a + b) / 2
                    xm = reactance(mid)
                    if xm < 0:
                        a, xa, xb = mid, xm, xb / 2
                    else:
                        b, xb, xa = mid, xm, xa / 2
                    if b - a < 0.02:
                        break
                hz = (a + b) / 2
                z = self.met_by_the_jet(hz, opened, far, mouth)
                slope = (reactance(hz + 0.5) - reactance(hz - 0.5))
                found.append(Resonance(hz, hz * slope / (2 * z.real) if z.real > 0 else float("inf")))
            f0, x0 = f1, x1
            f1 += step
        return found


def cents(hz: float, ref: float) -> float:
    return 1200 * math.log2(hz / ref)
