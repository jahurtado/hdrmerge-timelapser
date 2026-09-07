#!/usr/bin/env python3
"""Draw the application icon, one drawing per size.

    python tools/icon.py

Generated rather than committed as an opaque blob, so it can be argued with:
the shapes are here, in numbers, and a change is a diff. Same rule as
`eclipse-aligner`, whose icon this one has to live beside.

**The subject is a disc built out of exposure steps** -- the eclipse this tool
was written for, drawn as what the tool does to it: several exposures of one
scene, dark to light, resolved into a single image.

**The accent is amber, and that is a decision about the Dock, not about taste.**
`eclipse-aligner` is a white ring with a cyan crosshair. A second circular icon
in the same dark rounded square would be picked wrong at a glance, so the two
are separated by the one attribute that survives being 16 pixels tall: colour.
Amber is also what a corona actually looks like.

**Each size is drawn at its own size**, never reduced from the big one, because
the steps are the first thing to die. Four steps across a 512 px disc are 40 px
wide and obvious; across a 16 px disc they are 2 px and turn to mud. So the
count comes down with the size -- four, then three, then two at 16, where the
disc is simply dark on one side and light on the other, which is the whole idea
with everything unaffordable removed.
"""

import os

from PIL import Image, ImageDraw

BG = (18, 18, 22)             # the family's background
WHITE = (245, 246, 250)       # the brightest step
DARK = 38                     # the darkest step, per channel
ACCENT = (255, 176, 58)       # amber: the limb, and what tells us from the ring
SIZES = (16, 32, 64, 128, 256, 512)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "src", "hdrmerge_timelapser", "icons")
SS = 4                        # supersample, then shrink: the disc has to be round


def steps_for(size):
    """How many exposure steps the disc can hold at this size and stay legible."""
    return 4 if size >= 64 else 3 if size > 16 else 2


def icon(size):
    """One icon, drawn for `size`."""
    w = size * SS
    im = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, w - 1], radius=int(w * 0.22), fill=BG)

    c = w / 2
    r = w * (0.36 if size <= 16 else 0.34 if size <= 32 else 0.32)
    n = steps_for(size)

    # The ramp, drawn as bars and then cut to a circle. The bars overlap by a
    # pixel: at 512 a seam between two of them is a hairline of background, and
    # the eye finds it before it finds the picture.
    bars = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    db = ImageDraw.Draw(bars)
    for i in range(n):
        t = i / (n - 1)
        db.rectangle([c - r + 2 * r * i / n - 1, c - r,
                      c - r + 2 * r * (i + 1) / n + 1, c + r],
                     fill=tuple(int(DARK + (v - DARK) * t) for v in WHITE))
    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).ellipse([c - r, c - r, c + r, c + r], fill=255)
    im.paste(bars, (0, 0), mask)

    stroke = max(2 * SS, int(w * (0.034 if size <= 32 else 0.022)))
    d.ellipse([c - r, c - r, c + r, c + r], outline=ACCENT, width=stroke)
    return im.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for s in SIZES:
        p = os.path.join(OUT, "icon-%d.png" % s)
        icon(s).save(p)
        print("  %s" % os.path.relpath(p, HERE))
    icns = os.path.join(OUT, "icon.icns")
    icon(512).save(icns, format="ICNS",
                   sizes=[(s, s) for s in (16, 32, 128, 256, 512)])
    ico = os.path.join(OUT, "icon.ico")
    icon(256).save(ico, format="ICO", sizes=[(s, s) for s in SIZES if s <= 256])
    for p in (icns, ico):
        print("  %s" % os.path.relpath(p, HERE))


if __name__ == "__main__":
    main()
