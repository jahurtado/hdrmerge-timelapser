#!/usr/bin/env python3
"""Raw metadata reads that survive the DNGs Pillow will not open.

Pillow is the fast path and stays the default, but it refuses a whole class of
DNG this pipeline now has to read: a registered raw out of eclipse-aligner is an
uncompressed CFA image with no preview in IFD0, and Pillow rejects the *file*
("cannot identify image file") rather than just its pixels. tifffile reads the
same IFDs; its only quirk is that RATIONALs arrive flattened into
(num, den, num, den, ...), which is undone here so both paths hand back the same
plain floats.

Deliberately free of numpy: the analysis half of the pipeline (scan,
segmentation, --dry-run) depends on this module and should keep needing nothing
heavier than Pillow.
"""

from PIL import Image, ExifTags

# The IFD0 tags the develop needs, by the number Pillow keys them under.
COLOR_MATRIX_1 = 50721
COLOR_MATRIX_2 = 50722
AS_SHOT_NEUTRAL = 50728
CALIBRATION_ILLUMINANT_1 = 50778
CALIBRATION_ILLUMINANT_2 = 50779

_DNG_TAG_NAMES = {
    COLOR_MATRIX_1: "ColorMatrix1",
    COLOR_MATRIX_2: "ColorMatrix2",
    AS_SHOT_NEUTRAL: "AsShotNeutral",
    CALIBRATION_ILLUMINANT_1: "CalibrationIlluminant1",
    CALIBRATION_ILLUMINANT_2: "CalibrationIlluminant2",
}


def _unflatten(value, count):
    """A tifffile tag value as floats, undoing the flattened RATIONAL form.

    tifffile returns a RATIONAL of n values as 2n integers. len == 2 * count
    identifies that without depending on the dtype enum, and a division by zero
    denominator is a malformed tag, not a value worth propagating.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if len(value) == 2 * count:
        return [float(value[i]) / float(value[i + 1]) if value[i + 1] else 0.0
                for i in range(0, len(value), 2)]
    return [float(v) for v in value]


def exif_tags(path):
    """The raw's EXIF tags by name (DateTimeOriginal, ExposureTime, ISO, ...).

    Three readers, each for a file the one before it cannot open. Pillow is
    the fast path; tifffile takes the DNGs Pillow refuses (see the note at the
    top); and LibRaw takes the raws that are not TIFF at all -- a Canon CR3 is
    an ISO base-media container, so both of the others raise on the *file*,
    and a folder of them read as no raws at all.

    What LibRaw returns is what the analysis needs and no more: capture time,
    shutter, aperture and ISO. **Not exposure bias**, which it does not carry
    -- so a CR3 folder segments by the shutter-and-ISO ramp rather than by the
    bias tag, which is the same path the older Sigma material takes and is
    already the fallback for it.
    """
    try:
        exif = Image.open(path).getexif().get_ifd(ExifTags.IFD.Exif)
        if exif:
            return {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
    except Exception:                                       # noqa: BLE001
        pass
    try:
        import tifffile
        with tifffile.TiffFile(path) as t:
            tag = t.pages[0].tags.get("ExifTag")
            value = dict(tag.value) if tag is not None else {}
        # tifffile names the Exif tags exactly as Pillow does; only the
        # rationals need undoing, and a pair here is always one rational.
        return {k: (v[0] / v[1] if isinstance(v, tuple) and len(v) == 2
                    and all(isinstance(x, int) for x in v) and v[1] else v)
                for k, v in value.items()}
    except Exception:                                       # noqa: BLE001
        return _libraw_tags(path)


def _libraw_tags(path):
    """Capture data from LibRaw, for a raw that is not a TIFF underneath."""
    import rawpy

    with rawpy.imread(path) as raw:
        other = raw.other
    when = getattr(other, "timestamp", None)
    return {
        "DateTimeOriginal": when.strftime("%Y:%m:%d %H:%M:%S") if when else None,
        "ExposureTime": float(getattr(other, "shutter_speed", 0) or 0),
        "FNumber": float(getattr(other, "aperture", 0) or 0),
        "ISOSpeedRatings": float(getattr(other, "iso_speed", 0) or 0),
    }


def dng_tags(path):
    """The DNG color tags (matrices, illuminants, as-shot neutral) by number.

    Missing tags come back as None, which is also what a non-DNG raw yields --
    the develop falls back to LibRaw's camera database there.
    """
    try:
        exif = Image.open(path).getexif()
        return {num: exif.get(num) for num in _DNG_TAG_NAMES}
    except Exception:
        import tifffile
        with tifffile.TiffFile(path) as t:
            tags = t.pages[0].tags
            out = {}
            for num, name in _DNG_TAG_NAMES.items():
                tag = tags.get(name)
                out[num] = None if tag is None else _unflatten(tag.value, tag.count)
        return out


# What tells a raw from a picture, in the TIFF family. A DNG says so outright;
# a raw stored as plain TIFF carries the mosaic's description; and a CFA image
# says photometrically that its pixels are sensor sites and not colours.
DNG_VERSION = 50706
CFA_REPEAT_PATTERN_DIM = 33421
CFA_PATTERN = 33422
PHOTOMETRIC_CFA = 32803
PHOTOMETRIC_LINEAR_RAW = 34892

TIFF_LIKE = (".tif", ".tiff", ".dng")

# Formats that are a picture by definition -- no mosaic ever reaches them.
# None of these is in RAW_EXTS, so nothing in the pipeline asks today; the set
# is here so the question can be asked about any file and get a true answer,
# rather than the shrug an unknown extension deserves.
DEVELOPED = (".jpg", ".jpeg", ".png", ".exr", ".gif", ".bmp", ".heic",
             ".heif", ".webp", ".mov", ".mp4")


def is_raw(path):
    """Whether HDRMerge could actually merge this file.

    It merges a **mosaic**, one value per sensor site, choosing per pixel which
    exposure to take it from. A developed picture has no mosaic left, so
    HDRMerge refuses it -- and refuses it with `Error loading FILE, file not
    found`, which sends a person to look for a file that is right there. The
    point of asking here is to fail at the folder, in the words of the thing
    that is wrong.

    Only the TIFF family needs opening, and that is the whole reason for this:
    `.tif` is in the accepted extensions because some cameras write their raws
    that way, so a folder of developed TIFFs walks straight in. A `.nef` or a
    `.cr2` is a raw by its extension, and a `.jpg` is not one by its own.

    Cheap on purpose -- tags, never pixels -- and generous when it cannot
    tell: an unreadable file is somebody else's error to report, and saying
    "not a raw" about a raw would be the worse mistake.
    """
    import os

    ext = os.path.splitext(path)[1].lower()
    if ext in DEVELOPED:
        return False
    if ext not in TIFF_LIKE:
        return True
    try:
        import tifffile

        with tifffile.TiffFile(path) as t:
            for page in t.pages:
                tags = page.tags
                if tags.get("DNGVersion") is not None:
                    return True
                if (tags.get("CFARepeatPatternDim") is not None
                        or tags.get("CFAPattern") is not None):
                    return True
                photometric = tags.get("PhotometricInterpretation")
                if photometric is not None and int(photometric.value) in (
                        PHOTOMETRIC_CFA, PHOTOMETRIC_LINEAR_RAW):
                    return True
            return False
    except Exception:                                       # noqa: BLE001
        return True
