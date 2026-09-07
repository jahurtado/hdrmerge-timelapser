#!/usr/bin/env python3
"""Turn HDRMerge's floating-point DNGs into scene-linear frames.

Built for extreme-dynamic-range timelapses -- an eclipse -- that will be graded
in DaVinci Resolve. HDRMerge writes a float DNG anchored to the *shortest*
exposure of each bracket, with no BaselineExposure tag, so naive raw developers
(and LibRaw/rawpy) render it wrong. This reads the float CFA directly.

Per frame it:
  1. Reads the float CFA, black/white levels, AsShotNeutral (WB), and the EXIF
     of the reference (shortest) exposure that HDRMerge stamped on the DNG.
  2. White-balances, demosaics at full resolution (Menon by default; bilinear
     and Malvar are lighter), and converts camera RGB -> the chosen linear
     output space, Rec.709 by default.
  3. Normalizes to **absolute scene radiance** by dividing out the reference
     exposure (shutter x ISO / f-number^2). Across an eclipse this keeps the
     real brightness change continuous even though the shutter and ISO change.
     `apply` overrides that with a scale measured off the longest source,
     which survives per-frame pruning; see `apply.measured_scale`.

Resolve: import the EXR sequence, set the input/clip color space to Linear
with the matching gamut in Color Management, then grade.

This was a command of its own in the predecessor project. Here it is
the library `apply` calls, so it parses no arguments and prints nothing; what
it gained on the way in is `write_tiff` and `write_linear_dng`, for the output
formats that are not EXR.
"""

import os

import numpy as np
import tifffile

import imagecodecs
from scipy.ndimage import convolve

from . import rawmeta

XYZ2REC709 = np.array([  # linear sRGB / Rec.709 primaries, D65
    [3.2406, -1.5372, -0.4986],
    [-0.9689, 1.8758, 0.0415],
    [0.0557, -0.2040, 1.0570],
])
XYZ2P3D60 = np.array([  # DCI-P3 primaries, D60 (ACES) white
    [2.4027, -0.8975, -0.3881],
    [-0.8326, 1.7692, 0.0237],
    [0.0388, -0.0825, 1.0364],
])
XYZ2P3D65 = np.array([  # DCI-P3 primaries, D65 white — display P3
    [2.4935, -0.9314, -0.4027],
    [-0.8295, 1.7627, 0.0236],
    [0.0358, -0.0762, 0.9569],
])
XYZ2REC2020 = np.array([  # Rec.2020 primaries, D65 — the widest of the four
    [1.7167, -0.3557, -0.2534],
    [-0.6667, 1.6165, 0.0158],
    [0.0176, -0.0428, 0.9421],
])
# Selectable output spaces, all linear and scene-referred. Set the matching
# Input Colour Space in Resolve -- the EXR carries no `chromaticities`, so
# nothing downstream can work it out for itself.
#
# **Rec.709 is the default, and it is a measurement rather than a preference.**
# P3-D60 was the default for a while on the argument that a wider gamut cannot
# hurt. On this material it also cannot help: developed both ways, a totality
# frame and a transition frame put **0.000 %** of their lit pixels outside
# Rec.709 -- the corona is very nearly neutral, and even the reddest pixel of
# the chromosphere reaches only 2.8x the mean of its own channels. What the
# wider space does bring is a D60 white and a set of primaries every reader has
# to be told about. Rec.709 shares its primaries with sRGB, which is what
# everything assumes when it is not told.
OUTPUT_SPACES = {"rec709": XYZ2REC709, "p3-d65": XYZ2P3D65,
                 "p3-d60": XYZ2P3D60, "rec2020": XYZ2REC2020}
PHOTOMETRIC_CFA = 32803

# Reference exposure (ISO100, 1/100 s, f/2.8) -> normalization anchor so a
# reference-exposed frame lands near 0..1 before --exposure is applied.
READ_NOISE = 0.000107   # 1.75 levels of 16383, measured on the Sigma fp
E0 = 0.01 * 1.0 / (2.8 ** 2)


def cam_to_output(xyz2cam, xyz2out=XYZ2REC709):
    """Camera-RGB -> linear output space, rows normalised so neutral stays neutral.

    Row-normalisation makes the camera's neutral land on equal RGB, which is
    the output space's white by definition. It has a consequence worth stating
    because it decides one of the choices for you: **the white point of the
    output space cannot matter here**. P3-D60 and P3-D65 differ only in their
    white, and after this normalisation they produce identical numbers --
    verified on a totality frame, to three decimals, channel by channel. The
    choice that is left is the primaries.

    And what the primaries change is the *numbers*, not the colour. Wider
    primaries are more saturated, so less of them is needed for the same light:
    measured on one corona pixel, normalised to G=1, R comes out 1.555 in
    Rec.709, 1.441 in P3 and 1.311 in Rec.2020. Told which space it is, a
    reader shows the same colour from any of the three. Told the wrong one, it
    shows a picture desaturated by that difference -- 11 %% for P3 read as
    Rec.709, 24 %% for Rec.2020 -- which is the whole practical risk, and the
    reason to prefer the space everything assumes when it is told nothing.
    """
    m = xyz2out @ np.linalg.inv(xyz2cam)
    return m / m.sum(axis=1, keepdims=True)


def cam_to_rec709(xyz2cam):
    """Camera-RGB -> linear Rec.709 (back-compat shim for cam_to_output)."""
    return cam_to_output(xyz2cam, XYZ2REC709)


def _xyz2cam_from_dng_tags(path):
    """XYZ->camera matrix from a DNG's ColorMatrix tags, or None.

    Prefers ColorMatrix2 when it is the D65 calibration (illuminant 21), since
    the develop targets D65 Rec.709. Works for any DNG (Sigma, Ricoh, Adobe);
    NEF and other non-DNG raws don't carry these tags and return None.
    """
    try:
        tags = rawmeta.dng_tags(path)
    except Exception:
        return None
    cm1, cm2 = tags[rawmeta.COLOR_MATRIX_1], tags[rawmeta.COLOR_MATRIX_2]
    illum2 = tags[rawmeta.CALIBRATION_ILLUMINANT_2]
    cm = cm2 if (cm2 and illum2 == 21) else (cm1 or cm2)
    if not cm or len(cm) < 9:
        return None
    m = np.array([float(x) for x in cm[:9]], dtype=np.float64).reshape(3, 3)
    return m if np.linalg.matrix_rank(m) == 3 else None


def _xyz2cam_via_libraw(raw_path):
    """XYZ->camera matrix from LibRaw's camera database (covers NEF etc.)."""
    import rawpy
    with rawpy.imread(raw_path) as r:
        return np.array(r.rgb_xyz_matrix)[:3]


def xyz2cam_from_raw(raw_path):
    """Read the XYZ->camera color matrix from a raw file.

    Tries the DNG ColorMatrix tags first (Sigma/Ricoh/Adobe), then LibRaw's
    camera database (Nikon NEF, ...), so the develop uses the real sensor
    calibration of whatever camera shot the timelapse instead of a hardcoded
    model. Read from the *original* raw, not the merged float DNG, so it is
    immune to HDRMerge's color-tag relabelling (the Sigma fp green-tint bug)
    and to LibRaw's float-DNG mis-decode.
    """
    m = _xyz2cam_from_dng_tags(raw_path)
    return m if m is not None else _xyz2cam_via_libraw(raw_path)


def tag_scalar(tag, default):
    """First value of a TIFF tag as a float, whatever encoding it uses.

    BlackLevel is RATIONAL in Sigma's own DNGs and SHORT in HDRMerge's output.
    tifffile hands a RATIONAL back as a *flattened* (num, den, num, den, ...)
    tuple, so reading value[0] blindly yields 1048576 instead of 1048576/1024 =
    1024 -- which then subtracts the whole image to black. len(value) == 2*count
    identifies the flattened form without depending on the dtype enum.
    """
    if tag is None:
        return default
    v = tag.value
    if isinstance(v, (int, float)):
        return float(v)
    if not len(v):
        return default
    if len(v) == 2 * tag.count and float(v[1]):      # flattened rationals
        return float(v[0]) / float(v[1])
    first = v[0]
    if isinstance(first, tuple) and len(first) == 2 and float(first[1]):
        return float(first[0]) / float(first[1])    # unflattened rationals
    return float(first)


def read_cfa(path):
    """Return (cfa float32, black, white) from a raw's largest CFA image.

    Two readers, and the order is not a preference, it is a requirement.

    **tifffile first**, because HDRMerge writes a floating-point DNG and
    LibRaw decodes those to near-black -- about two distinct levels out of a
    frame that has hundreds. Every merge this program makes is such a file, so
    handing them to LibRaw would quietly ruin the picture instead of failing.

    **LibRaw second**, because tifffile only knows the compressions in the
    TIFF standard, and a camera raw is usually not one of them: a Nikon NEF
    stops it dead with `<COMPRESSION.NIKON_NEF: 34713> not supported`, and
    that killed the measure outright -- no plan written -- on a folder
    HDRMerge itself merges perfectly. Everything tifffile can read, it reads;
    what it cannot, LibRaw does.

    The two agree on what they return. LibRaw gives black per channel; where
    the four differ their mean is used, since everything downstream subtracts
    one number.
    """
    try:
        return _read_cfa_tiff(path)
    except Exception:                                       # noqa: BLE001
        return _read_cfa_libraw(path)


def _read_cfa_libraw(path):
    """The CFA of a camera raw tifffile cannot decompress."""
    import rawpy

    with rawpy.imread(path) as raw:
        cfa = raw.raw_image_visible.astype(np.float32)
        black = float(np.mean(raw.black_level_per_channel))
        white = float(raw.white_level)
    if cfa.ndim != 2:
        # Canon's sRAW and mRAW arrive as several components per site: they
        # are not a mosaic, they are a small colour image the camera made.
        # There is nothing to merge in one -- HDRMerge refuses them too, with
        # its usual "Error loading" -- and carrying it as if it were a CFA
        # would develop into nonsense. %s: what LibRaw actually handed over.
        raise ValueError("%s is not a Bayer mosaic (%d components per pixel) "
                         "— Canon sRAW/mRAW cannot be merged"
                         % (os.path.basename(path), cfa.shape[-1]))
    return cfa, black, white


def _read_cfa_tiff(path):
    """The CFA as the TIFF reader sees it.

    Where the CFA lives depends on who wrote the file: the camera and HDRMerge
    put a preview in IFD0 and the mosaic in a SubIFD, while a registered raw out
    of eclipse-aligner has the mosaic in IFD0 and no preview at all. Both are
    valid DNG, so every IFD is a candidate and the largest CFA wins -- picking
    the first would risk a reduced-resolution mosaic where one exists.
    """
    best = None
    with tifffile.TiffFile(path) as t:
        for page in t.pages:
            for s in [page] + list(page.pages or []):
                pi = s.tags.get("PhotometricInterpretation")
                if not pi or pi.value != PHOTOMETRIC_CFA:
                    continue
                if best is None or s.size > best.size:
                    best = s
        if best is None:
            raise ValueError("no CFA image found in %s" % path)
        return (best.asarray().astype(np.float32),
                tag_scalar(best.tags.get("BlackLevel"), 0.0),
                tag_scalar(best.tags.get("WhiteLevel"), 16383.0))


def _read_as_shot_neutral(path):
    """WB multipliers from a file's AsShotNeutral tag, or None if absent."""
    try:
        asn = rawmeta.dng_tags(path)[rawmeta.AS_SHOT_NEUTRAL]
    except Exception:
        return None
    if not asn:
        return None
    n = [asn[i] for i in (0, 1, 2)]
    wb = np.array([1.0 / n[0], 1.0 / n[1], 1.0 / n[2]])
    return wb / wb[1]


def read_meta(path, wb_source=None):
    """Return (wb[3], exposure_factor) from AsShotNeutral + reference EXIF.

    WB prefers `wb_source` (the original raw, before HDRMerge rewrote the color
    tags) when given, else `path`. The exposure factor is read from `path`'s
    reference EXIF (the shortest frame HDRMerge stamped on the merge).
    """
    wb = _read_as_shot_neutral(wb_source) if wb_source else None
    if wb is None:
        wb = _read_as_shot_neutral(path)
    if wb is None:
        wb = np.array([2.05, 1.0, 1.40])
    d = rawmeta.exif_tags(path)
    shutter = float(d.get("ExposureTime", 0.01) or 0.01)
    iso = float(d.get("ISOSpeedRatings", 100) or 100)
    fnum = float(d.get("FNumber", 2.8) or 2.8)
    exposure = shutter * (iso / 100.0) / (fnum ** 2)
    return wb, exposure


def demosaic_bilinear(cfa):
    """Bilinear RGGB Bayer demosaic at full resolution -> HxWx3 float32."""
    h, w = cfa.shape
    R = np.zeros_like(cfa); G = np.zeros_like(cfa); B = np.zeros_like(cfa)
    R[0::2, 0::2] = cfa[0::2, 0::2]
    G[0::2, 1::2] = cfa[0::2, 1::2]
    G[1::2, 0::2] = cfa[1::2, 0::2]
    B[1::2, 1::2] = cfa[1::2, 1::2]
    kg = np.array([[0, 1, 0], [1, 4, 1], [0, 1, 0]], np.float32) / 4.0
    krb = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32) / 4.0
    R = convolve(R, krb, mode="mirror")
    G = convolve(G, kg, mode="mirror")
    B = convolve(B, krb, mode="mirror")
    return np.stack([R, G, B], axis=-1)


# Malvar-He-Cutler (2004) gradient-corrected linear demosaic kernels (x1/8).
_MAL_G = np.array([[0, 0, -1, 0, 0], [0, 0, 2, 0, 0], [-1, 2, 4, 2, -1],
                   [0, 0, 2, 0, 0], [0, 0, -1, 0, 0]], np.float32) / 8.0
_MAL_ROW = np.array([[0, 0, 0.5, 0, 0], [0, -1, 0, -1, 0], [-1, 4, 5, 4, -1],
                     [0, -1, 0, -1, 0], [0, 0, 0.5, 0, 0]], np.float32) / 8.0
_MAL_COL = _MAL_ROW.T.copy()
_MAL_RB = np.array([[0, 0, -1.5, 0, 0], [0, 2, 0, 2, 0], [-1.5, 0, 6, 0, -1.5],
                    [0, 2, 0, 2, 0], [0, 0, -1.5, 0, 0]], np.float32) / 8.0


def demosaic_malvar(cfa):
    """Malvar-He-Cutler gradient-corrected RGGB demosaic -> HxWx3 float32.

    Less chroma speckle / zipper noise than bilinear at the same detail; still
    pure linear filtering (4 convolutions), no dependencies.
    """
    h, w = cfa.shape
    er = (np.arange(h)[:, None] % 2 == 0)
    ec = (np.arange(w)[None, :] % 2 == 0)
    r_site = er & ec            # red pixel
    b_site = (~er) & (~ec)      # blue pixel
    g_red = er & (~ec)          # green on a red row
    g_blue = (~er) & ec         # green on a blue row
    g = convolve(cfa, _MAL_G, mode="mirror")
    krow = convolve(cfa, _MAL_ROW, mode="mirror")
    kcol = convolve(cfa, _MAL_COL, mode="mirror")
    krb = convolve(cfa, _MAL_RB, mode="mirror")
    G = np.where(r_site | b_site, g, cfa)
    R = np.select([r_site, g_red, g_blue, b_site], [cfa, krow, kcol, krb])
    B = np.select([b_site, g_blue, g_red, r_site], [cfa, krow, kcol, krb])
    return np.stack([R, G, B], axis=-1).astype(np.float32)


def demosaic_menon(cfa):
    """Menon (2007) DDFAPD demosaic via the colour-demosaicing package.

    Highest quality (least noise/aliasing) of the three; slower than the linear
    methods. This is the default.
    """
    import warnings
    with warnings.catch_warnings():  # silence colour's matplotlib-missing notice
        warnings.simplefilter("ignore")
        from colour_demosaicing import demosaicing_CFA_Bayer_Menon2007
    return demosaicing_CFA_Bayer_Menon2007(cfa, "RGGB").astype(np.float32)


DEMOSAICS = {
    "bilinear": demosaic_bilinear,
    "malvar": demosaic_malvar,
    "menon": demosaic_menon,
}


def neutralize_highlights(rgb, sat, feather=6):
    """Pull blown highlights back to neutral instead of leaving them coloured.

    Every channel saturates at the same raw level, so a fully blown pixel leaves
    the sensor with R=G=B and the white balance then turns it into whatever the
    multipliers happen to say. On the eclipse frames that paints the clipped
    core of the chromosphere as a flat magenta band against a warm arc -- B/G
    jumps from 0.46 to 2.93 across a single pixel, and the values sit frozen at
    one colour for the width of the band. Forcing those pixels back to neutral
    turns the band into what a blown highlight should be, white-hot, and the
    feather keeps the boundary from being a step.

    `sat` is the saturation mask at CFA resolution; feathering it also covers
    the pixels the demosaic contaminated with clipped neighbours.
    """
    w = sat.astype(np.float32)
    if feather > 1:
        k = np.ones((feather, feather), np.float32) / (feather * feather)
        w = np.clip(convolve(w, k, mode="nearest") * 2.0, 0, 1)
    w = w[..., None]
    return rgb * (1.0 - w) + rgb.max(-1, keepdims=True) * w


def develop(path, cam2out, exposure_ev, wb_source=None, wb_override=None,
            demosaic="menon", highlight="neutral", highlight_feather=6,
            exposure_override=None, crop=None, with_levels=False, clamp=True):
    """Develop a raw or a merge to scene-linear RGB.

    With `with_levels`, also returns where the sensor's own limits landed in
    the output's units: `clip`, the value the white level became, and `noise`,
    the value one read-noise sigma became. They cannot be recovered from the
    picture afterwards -- the last line rescales everything to absolute
    radiance, so a short exposure comes out with a maximum of 24 and a long one
    with 0.3 -- and without them a window can show the data but cannot say
    which parts of it the camera failed to measure.
    """
    cfa, black, white = read_cfa(path)
    if crop:
        # Cropped before the demosaic and not after: developing 24 MP to show a
        # thumbnail of one corner costs two seconds, and almost all of it is
        # demosaicing pixels nobody will look at. The offsets are forced even so
        # the Bayer phase survives -- crop on an odd row and every colour moves.
        top, bottom, left, right = (v - v % 2 for v in crop)
        cfa = cfa[top:bottom, left:right]
    wb, exposure = read_meta(path, wb_source)
    if exposure_override:
        # HDRMerge normalizes every merge to the same output range whatever the
        # bracket's exposures (verified: two merges of one scene anchored 2 EV
        # apart agree to 0.1 EV at every percentile), so the reference exposure
        # it stamps carries no scale -- and it is not even stable, sometimes
        # naming the second step instead of the shortest. Dividing by it is what
        # produced the 3 EV of flicker between neighbouring merged frames.
        exposure = exposure_override
    if wb_override is not None:                      # fixed WB (R,G,B multipliers)
        wb = np.asarray(wb_override, dtype=np.float64)
        wb = wb / wb[1]                             # normalize to G=1
    sat = cfa >= white - 0.5                         # before black subtraction
    cfa = np.clip(cfa - black, 0, None)
    rgb = DEMOSAICS[demosaic](cfa)
    rgb *= wb.astype(np.float32)                    # white balance
    if highlight == "neutral" and sat.any():
        rgb = neutralize_highlights(rgb, sat, highlight_feather)
    lin = np.einsum("hwc,kc->hwk", rgb, cam2out.astype(np.float32))
    # Negatives are **kept** for a float output and clipped for an integer one.
    # A colour outside the output primaries comes out of the matrix with a
    # negative component; that is not an error and float can hold it -- clipped,
    # it is gone for good. Measured on merges of this material, above the
    # median brightness: 0.68 % of pixels have one in Rec.709, 0.28 % in P3-D65,
    # 0.10 % in Rec.2020, shallow (a few per cent of their own maximum) and in
    # the shadows. Small, and there is no reason to throw it away.
    if clamp:
        lin = np.clip(lin, 0, None)
    lin = lin / (white - black)                    # 0..~ relative to ref exposure
    scale = (E0 / exposure) * (2.0 ** exposure_ev)
    lin *= scale                                    # absolute radiance + user EV
    if with_levels:
        return lin, {"clip": float(scale), "noise": float(READ_NOISE * scale)}
    return lin


def write_exr(lin, out_path, bits=16):
    """Write a linear float image to a ZIP-compressed half/float EXR."""
    dtype = np.float16 if bits == 16 else np.float32
    data = imagecodecs.exr_encode(
        np.ascontiguousarray(lin.astype(dtype)), compression="zip")
    with open(out_path, "wb") as fh:
        fh.write(data)


def srgb_encode(x):
    """The sRGB transfer function. The real one, piecewise, not a 2.2 gamma."""
    a = np.clip(x, 0.0, 1.0)
    return np.where(a <= 0.0031308, a * 12.92,
                    1.055 * np.power(a, 1.0 / 2.4) - 0.055)


def write_tiff(lin, out_path, bits=16, curve="srgb", level=None):
    """Write an **integer** TIFF: what most photo software opens without asking.

    An integer file cannot hold a hundred stops, so writing one is choosing
    what to keep, and there are two honest answers:

    `srgb` encodes the scene against `level` with the standard sRGB transfer.
    It opens correctly anywhere, because sRGB is a thing a colour-managed
    program already knows.

    `linear` keeps the numbers and quantises them: black to `level` mapped
    straight onto the integers, nothing above. It opens dark and flat, because
    scene-linear data is dark and flat, and it is the right input for
    something that will do its own tone mapping.

    **Nothing from the window reaches either of them.** This used to write the
    picture as the editor was drawing it -- its log stretch, its 1/1.6 gamma,
    the exposure and local-contrast sliders -- and worse, the reference was
    the 99.7th percentile of *whichever frame happened to be on screen* when
    Process was pressed. So which picture you were looking at decided the
    brightness of the whole sequence. Controls for looking do not decide what
    is produced; `level` now comes measured from the run itself, one number
    for every frame, because a level measured per frame is flicker.
    """
    import tifffile

    peak = 65535 if bits == 16 else 255
    dtype = np.uint16 if bits == 16 else np.uint8
    scaled = np.clip(lin / (level or 1.0), 0.0, 1.0)
    data = (scaled if curve == "linear" else srgb_encode(scaled)) * peak
    tifffile.imwrite(out_path, np.rint(data).astype(dtype),
                     photometric="rgb", compression="zlib")


def write_linear_dng(merged_path, out_path, source_raw, read_cfa, bits=16):
    """Write the merge as an **integer DNG**: the one a raw editor opens.

    HDRMerge only writes floating-point DNGs, and a float DNG is what several
    raw editors either refuse or render wrong. This is the ordinary kind: the
    mosaic quantised linearly over its own black-to-white range, with
    `SampleFormat` back to unsigned integer and black at 0.

    **Uncompressed, and that is not an oversight.** It was written with zlib,
    which halves the file -- and LibRaw cannot read the result at all
    (`Corrupted data or unexpected EOF`, measured on both depths), because
    Deflate is not one of the compressions CinemaDNG allows. A DNG nothing can
    open is not worth half the bytes. Lossless JPEG is the compression that
    belongs here and `imagecodecs` can encode it, but tifffile has no writer
    for it, so it would mean assembling the TIFF by hand -- worth doing for
    the size, not worth guessing at now.

    `bits` is 14 or 16, and 14 is for **DaVinci Resolve**, which wants a raw
    that looks like a camera's. The values are written in a 16-bit container
    either way -- the DNG spec allows 8, 16 or 32 uncompressed and nothing
    else -- so what 14 changes is the range used and the white level declared,
    0..16383, exactly as a 14-bit camera writes its own raw.

    What either costs is shadow precision, and only there: the merged data is
    already clipped at the white level, so nothing is lost at the top. What is
    lost is the part of a float's range below one code value of the original
    scale, which is where a wide bracket's deepest shadows are.

    The colour tags come from the frame's own raw, which is also how the merge
    keeps its calibration: HDRMerge relabels the dual-illuminant matrices and
    the green cast that produces is a documented bug of this pipeline.
    """
    import tifffile

    from .align import _dng_tags

    if bits not in (14, 16):
        raise ValueError("an integer DNG is written at 14 or 16 bits, not %r"
                         % (bits,))
    top = (1 << bits) - 1
    cfa, black, white = read_cfa(merged_path)
    scaled = np.clip((cfa - black) / max(white - black, 1), 0.0, 1.0) * top
    tags = [t for t in _dng_tags(source_raw)
            if t[0] not in (50714, 50717, 50713)]     # black, white, its dim
    tags += [(50714, 3, 1, (0,), True),               # BlackLevel  = 0
             (50717, 3, 1, (top,), True)]             # WhiteLevel  = 16383/65535
    tmp = out_path + ".part"
    tifffile.imwrite(tmp, np.rint(scaled).astype(np.uint16), photometric="cfa",
                     planarconfig="contig", extratags=tags)
    if not _pack_lossless(tmp, out_path, bits):
        os.replace(tmp, out_path)
    return out_path


# The TIFF value types, by the code the tag carries, in bytes each.
TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
                  10: 8, 11: 4, 12: 8}


def _pack_lossless(plain_path, out_path, bits):
    """Rewrite an uncompressed DNG with its mosaic in lossless JPEG.

    The compression CinemaDNG actually allows, and the only one that buys
    anything here: Deflate makes the file unreadable and this makes it 27 MB
    where the plain one is 47, with the pixels identical to the byte -- not
    close, `array_equal`. DaVinci Resolve reads the result; so does LibRaw.

    tifffile has no writer for it, so the TIFF is assembled here: header,
    the encoded strip, then the image file directory. What it does *not* do is
    re-serialise the tags -- every value is copied across as the bytes it
    already is, so no colour matrix and no rational can be lost in
    translation. Only the three fields that depend on where the data sits are
    rewritten: the compression, and the strip's offset and length.

    That is also why it starts from a file tifffile has already written
    rather than from the tag list: laying out TIFF values is exactly the part
    worth borrowing from a library that does it correctly.

    False if it cannot be done -- an imagecodecs without the encoder -- and
    then the uncompressed file stands, which is bigger and just as correct.
    """
    import struct

    try:
        import imagecodecs

        with open(plain_path, "rb") as fh:
            raw = fh.read()
        with tifffile.TiffFile(plain_path) as t:
            page = t.pages[0]
            mosaic = page.asarray()
            entries = []
            for tag in page.tags.values():
                n = tag.count * TIFF_TYPE_SIZE.get(int(tag.dtype), 1)
                entries.append([tag.code, int(tag.dtype), tag.count,
                                raw[tag.valueoffset:tag.valueoffset + n]])
        blob = imagecodecs.ljpeg_encode(mosaic, bitspersample=bits)
    except Exception:                                       # noqa: BLE001
        return False

    by_code = {e[0]: e for e in entries}
    by_code[259][1:] = [3, 1, struct.pack("<H", 7) + b"\0\0"]   # JPEG
    for code, value in ((273, 8), (279, len(blob))):        # strip: where, how big
        entry = by_code.setdefault(code, [code, 4, 1, b""])
        entry[1:] = [4, 1, struct.pack("<I", value)]
    entries = sorted(by_code.values(), key=lambda e: e[0])

    ifd_at = 8 + len(blob)
    ifd_at += ifd_at % 2                       # every IFD starts on a word
    overflow = ifd_at + 2 + 12 * len(entries) + 4
    spill, body = b"", b""
    for code, dtype, count, value in entries:
        if len(value) <= 4:
            field = value + b"\0" * (4 - len(value))
        else:
            field = struct.pack("<I", overflow + len(spill))
            spill += value + (b"\0" if len(value) % 2 else b"")
        body += struct.pack("<HHI4s", code, dtype, count, field)

    tmp = out_path + ".ljpeg"
    with open(tmp, "wb") as fh:
        fh.write(struct.pack("<2sHI", b"II", 42, ifd_at))
        fh.write(blob)
        if (8 + len(blob)) % 2:
            fh.write(b"\0")
        fh.write(struct.pack("<H", len(entries)) + body + struct.pack("<I", 0))
        fh.write(spill)
    os.replace(tmp, out_path)
    os.remove(plain_path)
    return True


def develop_to_exr(dng_path, out_path, cam2out, exposure_ev=0.0, bits=16,
                   wb_source=None, wb_override=None, demosaic="menon"):
    """Develop one HDRMerge float DNG to a scene-linear EXR.

    `wb_source` is the original raw of the frame (for the un-mangled as-shot WB);
    `clamp` keeps or discards colours that fall outside the output primaries:
    off for a float file, which can carry them, on for anything integer.
    `wb_override` pins a fixed (R,G,B) white balance instead; `cam2out` is the
    camera-RGB -> output-space matrix (see `xyz2cam_from_raw` + `cam_to_output`);
    `demosaic` selects the algorithm (see DEMOSAICS). Used by the all-in-one path
    in hdrmerge_batch.py.
    """
    lin = develop(dng_path, cam2out, exposure_ev, wb_source=wb_source,
                  wb_override=wb_override, demosaic=demosaic)
    write_exr(lin, out_path, bits)
