#!/usr/bin/env python3
"""Lining up the steps of one bracket, before they are merged.

The frames of a bracket are the same scene four seconds apart, and on this
material they do not always land on the same pixels: measured on a real run,
steps sit **0 to 8 CFA pixels** from the longest one. Eight pixels at the
lunar limb is a double edge, which is exactly the artefact that sends somebody
looking for a bad blend radius.

HDRMerge has its own `--align`, and it is one line to switch on -- but it is a
median-threshold-bitmap aligner working on frames whose brightness differs by
three stops each, and it gives no number back and no way to correct it. Here
the offsets live in the plan, are shown per step, and can be moved by hand.

**Measured by phase correlation, with a confidence gate.** On the green plane
of the CFA, log-compressed, the correlation peak says how much to believe the
answer: a step carrying no signal correlates with nothing, and the 1/8000 of a
totality bracket answered "1704 pixels" with a peak of 0.009. Below the gate
the offset is left at zero and the step says so, which is the difference
between a tool that aligns and a tool that appears to.
"""

import os

MIN_PEAK = 0.02          # below this the correlation means nothing
MAX_SHIFT = 0.05         # of the frame; anything further is a mismeasurement
PLANE_STEP = 2           # decimation of the green plane, for speed


def _plane(path, step=PLANE_STEP):
    """One Bayer green plane, normalised, small enough to correlate fast."""
    import numpy as np

    from . import develop as dev

    cfa, black, white = dev.read_cfa(path)
    g = (cfa[0::2, 0::2].astype("float32") - black) / max(white - black, 1)
    return np.ascontiguousarray(g[::step, ::step])


def _peak_shift(a, b):
    """How far `b` is from `a`, and how much the answer is worth."""
    import numpy as np

    a = np.log1p(np.clip(a, 0, 1) * 500.0)
    b = np.log1p(np.clip(b, 0, 1) * 500.0)
    a = a - a.mean()
    b = b - b.mean()
    A, B = np.fft.rfft2(a), np.fft.rfft2(b)
    R = A * np.conj(B)
    R /= np.maximum(np.abs(R), 1e-9)
    c = np.fft.irfft2(R, a.shape)
    peak = np.unravel_index(int(np.argmax(c)), c.shape)
    dy = peak[0] - (a.shape[0] if peak[0] > a.shape[0] // 2 else 0)
    dx = peak[1] - (a.shape[1] if peak[1] > a.shape[1] // 2 else 0)
    return int(dx), int(dy), float(c.max()), a.shape


def measure(paths, reference, min_peak=MIN_PEAK):
    """Offsets of every path against `reference`, in CFA pixels.

    Returns {name: {"dx", "dy", "peak", "sure"}}. `sure` is False when the
    correlation is too weak or the answer is implausibly large; the offset is
    then zero, because a wrong shift is worse than none.
    """
    out = {}
    ref = _plane(reference)
    for path in paths:
        name = os.path.basename(path)
        if os.path.abspath(path) == os.path.abspath(reference):
            out[name] = {"dx": 0, "dy": 0, "peak": 1.0, "sure": True}
            continue
        dx, dy, peak, shape = _peak_shift(ref, _plane(path))
        # Back to CFA pixels: the plane is every other photosite, decimated
        # again, so one step there is four here -- and even, which is what
        # keeps the Bayer phase.
        dx, dy = 2 * PLANE_STEP * dx, 2 * PLANE_STEP * dy
        far = max(abs(dx) / (4 * shape[1]), abs(dy) / (4 * shape[0]))
        sure = peak >= min_peak and far <= MAX_SHIFT
        out[name] = {"dx": dx if sure else 0, "dy": dy if sure else 0,
                     "peak": round(peak, 4), "sure": bool(sure)}
    return out


# What makes the output a DNG rather than a TIFF full of numbers. Copied from
# the original tag by tag, with its own code, type and count, so nothing is
# re-derived on the way out.
DNG_TAGS = (
    271, 272,                       # Make, Model
    50706, 50707, 50708,            # DNGVersion, BackwardVersion, UniqueModel
    50721, 50722, 50723, 50724,     # ColorMatrix 1-2, CameraCalibration 1-2
    50727, 50728,                   # AnalogBalance, AsShotNeutral
    50778, 50779,                   # CalibrationIlluminant 1-2
    50730,                          # BaselineExposure
    50714, 50713, 50717,            # BlackLevel, its repeat dim, WhiteLevel
    33422, 33421, 50710,            # CFAPattern, its repeat dim, PlaneColor
    50829, 50719, 50720, 50718,     # ActiveArea, DefaultCrop*, DefaultScale
    50733,                          # BayerGreenSplit
)
# Without these the file opens and develops wrong, which is worse than failing.
ESSENTIAL = (50706, 50721, 50728, 33422, 50714, 50717)


def _dng_tags(path):
    """The original's DNG tags, ready to hand to `tifffile`.

    A camera keeps the mosaic and its levels in a SubIFD and the colour in
    IFD0, so both are searched rather than assuming one layout.
    """
    import tifffile

    out = []
    with tifffile.TiffFile(path) as t:
        pages = [t.pages[0]] + list(t.pages[0].pages or ())
        for code in DNG_TAGS:
            for page in pages:
                tag = page.tags.get(code)
                if tag is not None:
                    out.append((tag.code, int(tag.dtype), tag.count,
                                tag.value, True))
                    break
    missing = [c for c in ESSENTIAL if c not in {t[0] for t in out}]
    if missing:
        raise RuntimeError("%s is missing DNG tags %s; a shifted copy would "
                           "develop wrong rather than fail"
                           % (os.path.basename(path), missing))
    return out


def shift_cfa(cfa, dx, dy, order=1):
    """Move a mosaic by any amount, sub-plane by sub-plane.

    A Bayer mosaic cannot be resampled as one image: interpolating across
    neighbouring photosites mixes colours, and moving it by an odd number of
    pixels puts every photosite under a filter of a different colour. Each of
    the four sub-planes sits on its own grid of spacing 2, so moving the image
    by (dx, dy) means moving every sub-plane by (dx/2, dy/2) **of its own
    pixels**, and the interpolation only ever mixes photosites of one colour.

    An even shift lands on whole sub-plane pixels and is therefore exact --
    the same photosites in different places, nothing invented. Everything else
    is bilinear, which is the price of a quarter of a pixel.
    """
    import numpy as np
    from scipy.ndimage import shift as ndshift

    out = np.empty(cfa.shape, dtype=np.float32)
    for oy in (0, 1):
        for ox in (0, 1):
            plane = cfa[oy::2, ox::2].astype(np.float32)
            if dx % 2 or dy % 2:
                plane = ndshift(plane, (dy / 2.0, dx / 2.0), order=order,
                                mode="nearest")
            else:
                whole = np.zeros_like(plane)
                iy, ix = int(dy // 2), int(dx // 2)
                h, w = plane.shape
                ys, yd = ((slice(0, h - iy), slice(iy, h)) if iy >= 0
                          else (slice(-iy, h), slice(0, h + iy)))
                xs, xd = ((slice(0, w - ix), slice(ix, w)) if ix >= 0
                          else (slice(-ix, w), slice(0, w + ix)))
                whole[yd, xd] = plane[ys, xs]
                plane = whole
            out[oy::2, ox::2] = plane
    return out


def write_shifted(in_path, out_path, dx, dy):
    """Write a copy of a raw with its mosaic moved.

    The mosaic goes out uncompressed in one IFD, which is legal DNG and far
    less machinery than reproducing a camera's SubIFD-plus-previews layout. The
    embedded previews are dropped on purpose: they show the frame where it was.

    `eclipse-aligner` does a more capable version of this and it was imported
    for a while. Two tools that must not break each other are worth more than
    the fifty lines saved, so this is its own.
    """
    import subprocess
    import shutil

    import numpy as np
    import tifffile

    from . import develop as dev

    cfa, _, _ = dev.read_cfa(in_path)
    out = np.clip(np.rint(shift_cfa(cfa, dx, dy)), 0, 65535)

    tmp = out_path + ".part"
    tifffile.imwrite(tmp, out.astype("uint16"), photometric="cfa",
                     planarconfig="contig", compression=None,
                     extratags=_dng_tags(in_path))
    # The EXIF is a nested IFD and awkward to write by hand; exiftool carries
    # it across when it is there. Without it the file is still a correct DNG
    # and nothing downstream misses the tags: A/B'd on a five-frame bracket
    # with one frame shifted, the merged DNG is bit-identical either way --
    # HDRMerge estimates its exposure ratios from the pixels -- and so is the
    # EXR at the end of `apply`, which reads its scale off the *original*
    # longest frame. So the packaged app does not carry exiftool.
    tool = shutil.which("exiftool")
    if tool:
        from .preview import hidden_kwargs

        subprocess.run([tool, "-overwrite_original", "-q", "-q", "-unsafe",
                        "-tagsFromFile", in_path, "-EXIF:all", tmp],
                       capture_output=True, **hidden_kwargs())
    os.replace(tmp, out_path)
    return out_path


def shifted_source(cache, path, dx, dy):
    """A copy of one raw with its mosaic moved, made once and kept.

    Named after the file and the offset, so nudging a step by two pixels and
    back costs one write and not two.
    """
    name = "%s.%+.2f%+.2f.dng" % (os.path.splitext(os.path.basename(path))[0],
                                  dx, dy)
    out = os.path.join(cache, name)
    if not os.path.isfile(out):
        os.makedirs(cache, exist_ok=True)
        write_shifted(path, out, dx, dy)
    return out
