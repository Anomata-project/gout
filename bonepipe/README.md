# bonepipe

What a pipe sounds like, computed from its geometry. It is written for bone pipes from the
Palaeolithic, where little is certain: the first object is the perforated cave bear femur from
Divje babe I in Slovenia, 50 to 60 thousand years old, which may be a Neanderthal flute or a bone
chewed by a carnivore.

bonepipe lives in gout's repository in a folder of its own and uses nothing of gout: the Python
standard library is all it needs. gout has an instrument, `bonepipe`, that plays what it computes.

## Rules it keeps

- **No number is invented.** Every measurement in `data/` has the source it was read in, the page,
  the method (calipers, CT, an estimate) and a confidence. A source that was not read cannot carry
  a number: `python3 -m bonepipe check` fails if one does.
- **The same thing measured twice is there twice.** The holes are a millimetre or two larger by CT
  than by calipers; both stay, and a reconstruction says where between them it stands.
- **What is not known is a parameter with a range**, not a value: how long the tube was, which end
  was blown and how, how much the lips left open, whether the far end was open, whether the half
  holes were holes. Results are given over the whole range.
- **Nothing presumes that the object is an instrument.** Its status is `contested` in the data.
- **No scale is assumed.** Notes are frequencies; intervals are in cents, with the simple ratio
  nearest to each and how far off it is.

## What there is

```sh
python3 -m bonepipe objects                  # the objects
python3 -m bonepipe show divje-babe-1        # every measurement: value, method, source, page, confidence
python3 -m bonepipe bore divje-babe-1        # the marrow cavity along the bone, and where the holes are
python3 -m bonepipe unknowns divje-babe-1    # everything a reconstruction has to decide, with its range
python3 -m bonepipe notes divje-babe-1       # one reconstruction: its fingerings, their notes, the intervals
python3 -m bonepipe notes divje-babe-1 blown=distal far=closed hole5=yes mouth_open=0.02
python3 -m bonepipe spread divje-babe-1 -n 200   # the same over the whole range of what is not known
python3 -m bonepipe wav divje-babe-1 pipe.wav    # hear one reconstruction: its plain fingerings, a breath each
python3 -m bonepipe check                    # that the data holds together
```

`notes` answers for one reconstruction (x is a closed hole, o an open one, read from the mouth):

```
divje-babe-1  blown at the proximal end, 15% of it open; the other end open; hole1, hole2, hole3; 25 C
  fingering    lowest     nearest              step   nearest ratio
  xxx          1232.3 Hz  D#6-17   q  41
  xxo          1405.6 Hz  F6+11    q  43     +228 c   8/7 -3 c
  xoo          1946.8 Hz  B6-25    q  37     +564 c   11/8 +13 c
  ooo          2545.8 Hz  D#7+39   q  27     +464 c   9/7 +29 c
```

That is not "the scale of the flute". `spread` draws reconstructions from the whole range of what
is not known and shows how far each of those numbers moves: with 120 draws the all-closed note
alone ranges over an octave or more in every way of blowing, and each step over several hundred
cents. Any claim about intervals has to survive that.

How far single unknowns move the four notes above, each taken alone across the range given:

| unknown | from, to | moves the notes by |
| --- | --- | --- |
| the lips: share of the blown end left open | 5%, 40% | +181 to +554 cents |
| the missing length | none, 30 mm at each end | -449 to -636 cents |
| the silhouettes' edge (`edge_mm`) | 0.4, 1.0 mm | -16 to -119 cents |
| the air's temperature | 15, 35 C | +57 cents |
| the complete holes' size | calipers, CT | -6 to +46 cents |

The acoustic model itself agrees with an independent one within 7 cents (below). What limits what
can be said is the reconstruction, not the acoustics.

## The object as it is

The femur is 113.6 mm long and broken at both ends. It is a closed tube for 40 mm only, from 28.5
to 68.5 mm from the proximal end: proximally the posterior wall is gone from the half hole on, and
distally the anterior wall is gone from the other half hole on. It cannot sound as it lies in the
museum. Every sounding version of it is a restoration, and the published ones differ in which end
is blown, in whether a stopper is fitted, and in what the hands do at the far end.

## Where the bore comes from

Turk, Pflaum and Pekarovič (2005) print 108 transverse slices of their CT scan as silhouettes at
1:1, each with its number. `tools/ct_slices.py` reads them out of the paper's PDF (you download it
yourself; the paper is open access) and measures each one; the result is
`data/divje-babe-1.slices.json`. Where the bone is a closed ring away from the holes, the cavity's
area is measured (38 slices, from 29 to 67.5 mm). Where a hole or a break opens the ring, the width
between the side walls is still measured and the depth is estimated. Past the slices that were
printed, the bore is continued by a parameter.

Two things were checked against the calipers. The slices are 0.5 mm apart: that puts the two
complete holes 34 mm apart (35 mm by calipers), the half hole 17.7 mm from its neighbour (18 mm),
and the proximal edges of the posterior and the anterior hole 3 mm apart (3 to 4 mm in the paper).
And the silhouettes are wider than the bone, by half a millimetre on each surface going by the
shaft's caliper widths and by 0.4 to 1.0 mm going by the paper's own wall and hole values: that
is the parameter `edge_mm`, and it moves the notes by up to a little over a semitone.

When the scan itself is available, a bore profile from it replaces these stations and the rest
stays as it is.

## The acoustics

`acoustics.py` is a transfer-matrix model of plane waves in the bore: wall losses, side holes as
T-sections with the usual length corrections (Lefebvre and Scavone 2012; Nederveen et al. 1998;
Dubos et al. 1999), radiation at open ends, and the blown end as an opening smaller than the bore
in series with it. A flute sounds near the frequencies where the impedance the jet meets is
smallest; those are the resonances reported, with a sharpness `q`.

How it was checked, in `tests/test_bonepipe.py`:

| against | result |
| --- | --- |
| an open tube, a stopped tube and a bottle, from their equations | within 2 cents for the tubes once the walls' slowing of sound is counted (19 cents by itself); the bottle within 45 cents below its lossless equation |
| OpenWInD 0.12.4, finite elements, on two tubes the size of the find with two holes, four fingerings each | all 16 resonances within 7 cents |
| the published calculation of Dimkaroski's copy (Horusitzky 2014): three tones | within 43, 61 and 45 cents |

What was not checked: any sounding tube. The holes are nearly as wide as the bore and close
together, at the edge of what the hole formulas were fitted for; holes this close also act on each
other outside the tube, which the model leaves out; and the sounded pitch lies some tens of cents
from the resonance, depending on the lips and the breath. A printed tube of known size, recorded,
would settle the first two.

## Hearing it

`sound.py` makes a note from what the acoustics computes for a fingering, and from nothing else:
the note is the resonance, each overtone is as strong as the pipe lets it through (the impedance
the jet meets at that overtone, against the impedance at the note), and the breath is noise
through the same resonances. No recording is used and the jet at the lips is not simulated. For
this pipe the overtones come out weak, because its resonances are far from being multiples of
each other: it sounds plain, close to a whistle. That much follows from the geometry; what a
player's lips and breath add does not, and is not in it.

```sh
python3 -m bonepipe wav divje-babe-1 pipe.wav
python3 -m bonepipe wav divje-babe-1 bottle.wav far=closed mouth_open=0.02
```

In gout the same sound is an instrument. A step says how it is blown and which holes are open,
the first row's cells are the reconstruction, and the grid shows the notes that follow. In gout's
ui, `ctrl-y` opens that grid as a sheet to move about in and fill:

```sh
gout instrument add bonepipe bone
gout ins bone blow 1 soft = = =
gout ins bone hole2 3-4 o
gout ins bone far closed
gout ins bone
```

## What comes next

Player models (a Neanderthal and a sapiens hand and breath) from fossil data; null models (holes
where fingers fall, holes where teeth bite) and the statistics against them; the same pipeline on
Hohle Fels, Geißenklösterle and Jiahu, and on bitten bones as controls; a jet at the lips, so that
blowing harder changes the note as it does on a real pipe; a mesh to print.

## Sources read

`python3 -m bonepipe show divje-babe-1` lists them with their addresses. The measurements come
from Turk, Dirjec and Kavur (1997) and Turk, Pflaum and Pekarovič (2005); the ways of blowing from
Kunej (1997) and Dimkaroski (2014); the calculation used as a check from Horusitzky (2014); the
date from Turk, Turk and Toškan (2016). Tuniz et al. (2012), a micro-CT, was not read and carries
no number here.
