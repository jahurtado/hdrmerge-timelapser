#!/usr/bin/env python3
"""Read a folder of raws and write the plan of what to merge.

Nothing here writes a pixel. It reads every frame's EXIF, works out where one
bracket ends and the next begins, decides for each of them which steps are worth
merging and how wide HDRMerge's layer mask should be blurred, and puts all of it
in a document with the measurement beside every decision.

Every number in this module came out of measurements on real eclipse material;
the docstrings carry the reason each rule exists rather than another, at the
point of use.
"""

import datetime
import glob
import os
import sys
from collections import Counter

from . import plan
from .rawmeta import exif_tags, is_raw

RAW_EXTS = {".dng", ".nef", ".cr2", ".cr3", ".arw", ".raf", ".rw2", ".orf",
            ".pef", ".srw", ".tif", ".tiff"}

KIND_SINGLE = "single"        # a lone frame in the timelapse's one-shot config
KIND_LADDER = "ladder"        # a complete bracket at the ordinary cadence
KIND_SYNC = "sync"            # a complete bracket fired from the camera buffer
KIND_TRUNC = "truncated"      # a bracket cut short
KIND_ORPHAN = "orphan"        # ring fillers, one-step stubs, anything else
ANOMALOUS = frozenset({KIND_SYNC, KIND_TRUNC, KIND_ORPHAN})

MAX_STEP_SECONDS = 4.0        # a gap this wide ends a burst even mid-ramp
SYNC_MAX_SECONDS = 2.0        # a complete bracket this quick came from the buffer
SYNC_FAST_SECONDS = 0.5       # dead time per frame: fired back to back
SYNC_NORMAL_SECONDS = 2.0     # dead time per frame: the ordinary cadence
MIN_OWN_STEPS = 2             # fewer own steps than this: drop, do not rebuild
MAX_BORROW_SECONDS = 8.0      # only borrow from a ladder this close in time
MAX_BRACKET_EV_STEP = 2.0     # wider steps make dark halos around bright edges

# How wide `auto` may go, by how far the bracket jumps between one frame and
# the next. A wide blur is what removes the dark halo a big step leaves around
# a bright edge -- and where there is no such step there is no halo to remove,
# so widening only buys the risk of dragging a dark frame's noise across the
# picture. Measured on a run whose steps are 3.06 EV: of the
# twenty bracketed sets, the radius set by hand was 50 on eight of them and 3
# on the nine of totality, which is what this ladder says.
RADIUS_BY_EV = ((2.0, 3), (2.5, 10), (float("inf"), 50))

MIN_STEP_COVERAGE = 0.0001    # 0.01 % of the frame
SATURATION = 0.995            # of the black-to-white range
READ_NOISE = 0.000107         # 1.75 levels of 16383, measured on the Sigma fp
USABLE_SNR = 10.0
# The ladder of blur radii, in the sizes a person expects to see rather than
# the four that happened to come out of the investigation. HDRMerge's own
# default is 3; 500 is past anything this material has wanted.
RADIUS_CANDIDATES = (3, 5, 10, 25, 50, 100, 250, 500)

# The widest radius `auto` may choose on its own. The score below only ever
# *vetoes* a radius -- it measures the noise a wide mask drags in, and it says
# nothing about the halo a narrow one leaves, which is the reason to go wide at
# all. So on a frame where every candidate scores 0.000 %, "the widest that
# nothing objects to" is really "the last entry in the list", and lengthening
# the list would silently move every one of those frames. 250 is where the
# investigation landed (300, measured, on the transition) and it merges in half
# the time 500 does. Wider is still one click away, by hand.
RADIUS_AUTO_MAX = 250
RADIUS_BUDGET = 0.001         # 0.1 % of the frame


class Shot:
    """One raw file, and the four things about it that decide anything."""

    __slots__ = ("path", "name", "time", "exp", "fnum", "iso", "ec")

    def __init__(self, path, time, exp, fnum, iso, ec):
        self.path = path
        self.name = os.path.basename(path)
        self.time = time
        self.exp = exp
        self.fnum = fnum
        self.iso = iso
        self.ec = ec


def shutter(seconds):
    """A shutter speed as a photographer writes it: 1/125, 0.5, 2."""
    if not seconds:
        return "0"
    if seconds >= 1:
        return ("%g" % seconds)
    return "1/%g" % round(1.0 / seconds)


def exposure_factor(shot):
    """Relative exposure of a shot: shutter x ISO / aperture^2."""
    return shot.exp * (float(shot.iso or 100) / 100.0) / ((shot.fnum or 2.8) ** 2)


def eff_exposure(shot):
    """Shutter x ISO -- the metric a bracket ramps along."""
    return shot.exp * float(shot.iso or 100)


def burst_span(burst):
    """Seconds from the first frame of a burst to the last."""
    return (burst[-1].time - burst[0].time).total_seconds()


def read_shot(path):
    """A Shot from a raw's EXIF, or None when it has no capture time."""
    d = exif_tags(path)
    raw_time = d.get("DateTimeOriginal")
    if not raw_time:
        return None
    return Shot(
        path,
        datetime.datetime.strptime(str(raw_time), "%Y:%m:%d %H:%M:%S"),
        float(d.get("ExposureTime", 0) or 0),
        float(d.get("FNumber", 0) or 0),
        d.get("ISOSpeedRatings"),
        float(d.get("ExposureBiasValue", 0) or 0),
    )


def scan(folder, report=print, ignored=None):
    """Every raw in a folder, in filename order, with its EXIF read.

    Filename order and not capture time: `DateTimeOriginal` on the Sigma fp is
    unreliable and not monotonic with the files -- jumps of twenty minutes
    between consecutive frames were measured -- which made time-gap segmentation
    cut the run into singletons and merge nothing.
    """
    files = sorted(p for p in glob.glob(os.path.join(folder, "*"))
                   if os.path.splitext(p)[1].lower() in RAW_EXTS)
    shots = []
    for p in files:
        # Asked before the metadata is read, because a developed picture can
        # carry a full set of it. A TIFF out of Lightroom has a capture time,
        # a shutter speed and an ISO, and would walk all the way to the merge
        # to be refused there -- by HDRMerge, in the words `Error loading
        # FILE, file not found`, about a file that is plainly there.
        if not is_raw(p):
            report("  ! skip %s: not a raw — the merge needs the sensor "
                   "mosaic, and a developed picture has none left"
                   % os.path.basename(p))
            if ignored is not None:
                ignored.append({"file": os.path.basename(p),
                                "why": "not a raw"})
            continue
        try:
            s = read_shot(p)
        except Exception as e:                                  # noqa: BLE001
            report("  ! skip %s: %s" % (os.path.basename(p), e))
            continue
        if s is None:
            report("  ! skip %s: no capture time" % os.path.basename(p))
            if ignored is not None:
                ignored.append({"file": os.path.basename(p),
                                "why": "no capture time"})
            continue
        shots.append(s)
    return shots


def has_exposure_bias(shots):
    """True when the camera actually records the bracket in the bias tag."""
    return len({round(s.ec, 2) for s in shots}) > 1


def segment_by_ec(shots, max_step_seconds=MAX_STEP_SECONDS):
    """Split into bursts by the exposure-bias pattern.

    **A bracket does not repeat a step.** Whatever order a camera fires them
    in, each notch of the bias appears once per burst, so the burst ends where
    a value comes round again -- and that is the whole rule, plus the clock.

    It used to read the *ramp*: a burst was a maximal monotonic run, in either
    direction, since an ordinary ladder climbs dark to bright and a burst
    fired from the buffer can come out bright to dark. That works only for a
    camera that steps one notch at a time in one direction, and Nikon does
    not: it fires in metering order -- the meter's reading, then under, then
    over, a bias of -0.67, -2.67, +1.33. Two frames descend and the third
    reverses, so every burst of three was cut into a pair and an orphan, and
    the frame thrown away was always the over-exposed one, the only one
    holding the shadows. Twelve frames became four merges of the two short
    exposures.

    Repetition sees the boundary the ramp cannot, and it sees the ones the
    ramp saw. Measured frame by frame, this returns *exactly* what the ramp
    rule returned on both Sigma sets -- the 10-frame test folder and the
    525-frame eclipse, ladders, buffer bursts and interleaved singles
    included -- and 4 bursts of 3 on the Nikon material, where the ramp gave
    8 broken ones.

    A gap longer than `max_step_seconds` still ends a burst, and it is not
    redundant: it is what separates a passthrough single from the ladder that
    follows it, when the single's bias is a notch the ladder also uses. On
    that material dropping the clock turns a five-step ladder into a three.

    The direction is not consulted at all now, which is why a descending
    burst -- the diamond-ring one ramps +6 to -6 -- survives without a clause
    of its own.
    """
    if not shots:
        return []
    bursts, seen = [[shots[0]]], {round(shots[0].ec, 2)}
    for prev, s in zip(shots, shots[1:]):
        notch = round(s.ec, 2)
        stalled = (s.time - prev.time).total_seconds() > max_step_seconds
        if stalled or notch in seen:
            bursts.append([])
            seen = set()
        bursts[-1].append(s)
        seen.add(notch)
    return bursts


def segment_by_cycle(shots):
    """Split by the shutter x ISO ramp, for cameras that pin the bias to 0."""
    bursts, current = [], []
    for s in shots:
        if current and eff_exposure(s) <= eff_exposure(current[-1]):
            bursts.append(current)
            current = []
        current.append(s)
    if current:
        bursts.append(current)
    return bursts


def segment_by_gap(shots, seconds):
    """Split where the clock stops. The escape hatch, and only on request.

    It cannot be the general rule: measured on the 525-frame eclipse, where
    the engine fires ladder after ladder with no pause between them, a
    four-second gap returns bursts of 18, 45 and 25 frames. But where a camera
    really does pause between brackets -- 0 s inside the burst and 9 to 123 s
    between them, on the Nikon material -- it is the plainest signal there is,
    and it needs no bias tag at all.
    """
    if not shots:
        return []
    bursts = [[shots[0]]]
    for prev, s in zip(shots, shots[1:]):
        if (s.time - prev.time).total_seconds() > seconds:
            bursts.append([])
        bursts[-1].append(s)
    return bursts


def segment(shots, gap=None):
    """Bursts, and the name of the method that found them."""
    if gap:
        return segment_by_gap(shots, gap), "%.1f s gap, filename order" % gap
    if has_exposure_bias(shots):
        return segment_by_ec(shots), "exposure-bias pattern, filename order"
    return segment_by_cycle(shots), "shutter x ISO cycle, filename order"


def signature(seq):
    """A hashable fingerprint of a burst's configuration."""
    return (len(seq), seq[0].fnum, seq[0].iso,
            tuple(shutter(s.exp) for s in seq))


def bracket_key(seq):
    """A burst's configuration, independent of the order it was shot in.

    A synchronized burst that ramps the other way is the same set of exposures
    and a perfectly good donor, but its ordered `signature` differs.
    """
    return (len(seq), seq[0].fnum, seq[0].iso,
            tuple(sorted((s.ec, shutter(s.exp)) for s in seq)))


def sync_class(burst):
    """How tightly a bracket was fired: "fast", "normal" or "slow".

    What is measured is the dead time -- the span from the first frame to the
    last, minus the exposures themselves, which the camera cannot help
    spending. A bracket ending 1/2000 . 1/4 spends 2.3 s of that span simply
    holding the shutter open, and calling it slow for it would be measuring
    the subject, not the camera.

    The old `sync` label put a flat 2 s on the span and meant "fired from the
    buffer". That is a *cause*, and only one of the causes: a faster camera, a
    burst mode, a shorter interval all produce the same tightness. This says
    what it can see.

    Both thresholds are per frame, which is the part worth insisting on: a
    flat "under 3 s in the whole bracket" reads a three-frame bracket shot at
    the ordinary cadence -- 1.7 s of dead time -- as fired back to back, and
    the label would then say fast about the shortest brackets in any folder.

    Measured on a real run: the three buffer bursts come out fast at
    0.08 s of dead time per frame (the earthshine one negative -- the
    timestamps have one-second resolution), and every ordinary ladder normal
    at 0.68-0.88 s.
    """
    if len(burst) < 2 or not all(s.time and s.exp for s in burst):
        return None
    dead = (burst_span(burst) - sum(s.exp for s in burst))
    if dead < SYNC_FAST_SECONDS * len(burst):
        return "fast"
    if dead < SYNC_NORMAL_SECONDS * len(burst):
        return "normal"
    return "slow"


def expected_frames(bursts):
    """How many frames each bracket should have, from the brackets like it.

    The old rule was the mode of the whole folder, which is right only when
    the folder shoots one bracket length. A run that shoots five frames
    through totality and three either side would have four dozen perfectly
    complete brackets called truncated.

    "Like it" is defined by the exposures themselves: two brackets belong to
    the same family when one's set of (ISO, shutter) is a subset of the
    other's -- which is exactly what a bracket cut short is, a subset of the
    one it was cut from, whichever way the ramp runs. Within a family the
    expected length is the commonest, ties going to the longer. A bracket
    with exposures of its own -- the earthshine burst, 1/2000 to 2 s -- is a
    family of one and is complete by definition, which is the honest answer:
    nothing in the folder says otherwise.

    Returns one number per burst, None for the ones that are not brackets.
    """
    sets = [frozenset((s.iso, shutter(s.exp)) for s in b) if len(b) > 1 else None
            for b in bursts]
    out = []
    for mine in sets:
        if mine is None:
            out.append(None)
            continue
        family = Counter(len(other) for other in sets
                         if other is not None
                         and (other <= mine or mine <= other))
        out.append(max(family, key=lambda n: (family[n], n)))
    return out


def classify(bursts, sync_max_seconds=SYNC_MAX_SECONDS,
             max_step_seconds=MAX_STEP_SECONDS):
    """Label every burst, and report the ladder length the run settles on.

    `full_n` is the modal bracket length. A burst of one frame is a
    **passthrough frame of the timelapse** unless it is the leftover of a
    bracket, and the leftover is recognised for what it is: a step of the
    ladder standing next to it, taken within that ladder's own rhythm.

    Both halves of that test are load-bearing. The exposure alone is not
    enough -- a passthrough frame can happen to be shot at a shutter the
    bracket also uses -- and the clock alone is not either. Together they
    describe a fragment of a burst and nothing else: same step, and a second
    away rather than an interval away.

    What this replaces was a *configuration*: the one-frame signature that
    recurs most, everything else an orphan and dropped. That reads a timelapse
    whose light never changes, and a timelapse whose light never changes is
    not one anybody shoots. Measured on the eclipse run: 247 frames before
    totality at 1/1000, then the light comes back and the camera opens --
    1/250, 1/125, 1/100, 1/13 -- and every one of those 187 frames was thrown
    away for not being 1/1000. Half the sequence, gone for having been
    exposed correctly.
    """
    lengths = Counter(len(b) for b in bursts if len(b) > 1)
    full_n = lengths.most_common(1)[0][0] if lengths else 1
    kinds = []
    for i, burst in enumerate(bursts):
        if len(burst) == 1:
            kinds.append(KIND_ORPHAN if _bracket_leftover(
                bursts, i, max_step_seconds) else KIND_SINGLE)
        elif len(burst) == full_n:
            kinds.append(KIND_SYNC if burst_span(burst) <= sync_max_seconds
                         else KIND_LADDER)
        else:
            kinds.append(KIND_TRUNC)
    return kinds, full_n, None


def _bracket_leftover(bursts, i, max_step_seconds):
    """Is this lone frame a piece of the bracket beside it, rather than a frame?"""
    lone = bursts[i][0]
    mine = (round(lone.exp, 6), lone.iso)
    for j in (i - 1, i + 1):
        if not (0 <= j < len(bursts)) or len(bursts[j]) < 2:
            continue
        steps = {(round(s.exp, 6), s.iso) for s in bursts[j]}
        nearest = bursts[j][-1] if j < i else bursts[j][0]
        gap = abs((lone.time - nearest.time).total_seconds())
        if mine in steps and gap <= max_step_seconds:
            return True
    return False


def find_donor(bursts, kinds, index, max_borrow_seconds=MAX_BORROW_SECONDS):
    """The complete burst best placed to lend a truncated one its missing steps.

    Steps are matched by exposure bias, not by position: a ladder normally loses
    its slow tail, but nothing guarantees it was stepped dark to bright, so which
    notches are missing is read from the bias values actually present. Distance
    is measured frame to frame and not burst start to burst start, because a
    ladder takes 4-5 s -- more than the tolerance itself.
    """
    trunc = bursts[index]
    ref = next((b for b, k in zip(bursts, kinds) if k not in ANOMALOUS), None)
    if ref is None or len(trunc) >= len(ref):
        return None, None, None
    want = bracket_key(ref)
    have = {s.ec for s in trunc}
    best = best_lent = best_gap = None
    for b, k in zip(bursts, kinds):
        if k not in (KIND_LADDER, KIND_SYNC) or bracket_key(b) != want:
            continue
        lent = [s for s in b if s.ec not in have]
        if len(lent) + len(trunc) != len(b):
            continue
        gap = max(abs((s.time - trunc[-1].time).total_seconds()) for s in lent)
        if best_gap is None or gap < best_gap:
            best, best_lent, best_gap = b, lent, gap
    if best is None or best_gap > max_borrow_seconds:
        return None, None, best_gap
    return best, best_lent, best_gap


def middle_shot(win):
    """The shot a merge is attributed to: the one in the middle of the run.

    By position, not by clock. The merge mixes the whole ladder, so the instant
    it stands for is the middle of it, and naming it after the first frame dates
    every output half a bracket early -- 2.5 s on a five-step ladder. Picking the
    frame nearest the window's mid-time sounds better but collides: timestamps
    have one-second resolution and several frames of a burst share a second.
    """
    return win[len(win) // 2]


def step_coverage(shots, read_cfa):
    """Per step of a bracket, the share of the frame it is the one to *resolve*.

    Walking from the longest exposure down, a step serves the pixels every
    longer step has already blown -- and it only counts where it can see what
    it serves, SNR over `USABLE_SNR`, the same floor the radius scoring uses.

    That second half was missing and it let a frame of pure noise through.
    Measured on one bracket: the 1/8000 is the only step not blown on
    the crescent, so by saturation alone it "serves" 0.313 % of the picture --
    over the 0.01 % floor, kept, merged. But **its median SNR where it serves
    is 0.0**: at 1/8000 and ISO 100 the crescent is at the noise floor. The
    other four steps score in the thousands. What that frame contributed was
    speckle along the whole crescent, and a wide blur radius spread it.

    Counting only what a step can see, it serves 0.0000 % and is dropped, and
    the four that carry signal keep their numbers to four decimals.
    """
    import numpy as np

    order = sorted(shots, key=exposure_factor, reverse=True)
    out, need = [], None
    for s in order:
        cfa, black, white = read_cfa(s.path)
        x = ((cfa[::4, ::4] - black)              # a share of a frame is a
             / float(white - black)).astype(np.float32)          # statistic
        sat = x >= SATURATION
        served = ~sat if need is None else (need & ~sat)
        seen = np.maximum(x, 0) / READ_NOISE > USABLE_SNR
        out.append((s, float((served & seen).mean())))
        need = sat if need is None else (need & sat)
    return out


def useful_steps(shots, coverage, min_coverage=MIN_STEP_COVERAGE):
    """Which steps are worth merging, and what each one is worth.

    A step nothing needs is not spare range, it is a liability. HDRMerge blends
    a layer in wherever the neighbouring one clips, so a step carrying no signal
    contributes amplified noise; and the merge is anchored on the shortest layer
    included, so adding an empty one moves the whole frame's scale by 2.3-3 EV.
    Steps are dropped from the short end only and never below two.
    """
    keep, dropped = [coverage[0]], []
    for shot, served in coverage[1:]:
        if served < min_coverage and len(keep) >= 2:
            dropped.append((shot, served))
            continue
        keep.append((shot, served))
    kept_names = {s.name for s, _ in keep}
    # Anything shorter than the first drop goes too, in the order it was shot.
    dropped += [(s, v) for s, v in coverage
                if s.name not in kept_names
                and s.name not in {d.name for d, _ in dropped}]
    return keep, dropped


def radius_cap(shots, ladder=RADIUS_BY_EV):
    """How wide `auto` may go on this bracket, from the size of its steps."""
    worst = max(bracket_ev_steps(shots), default=0.0)
    for limit, cap in ladder:
        if worst <= limit:
            return cap
    return ladder[-1][1]


def auto_radius(scores, cap, budget=RADIUS_BUDGET):
    """The widest candidate that stays under budget, and never wider than cap.

    Two things this is not. It is not "the first one that fails, minus one":
    the score is **not monotonic in the radius** -- on this material r3 spoils
    0.136 % of the picture and r10, r50 and r250 spoil 0.000 %, because at
    three pixels the mask is barely blurred and the seam itself is what
    spoils. Walking up and stopping at the first failure returned 3 there
    while the window explained, correctly, that the widest under budget was
    250. Two definitions of one word, and the window's was the right one.

    And it is not unbounded: `cap` comes from the bracket's own EV step, so a
    gentle bracket keeps the default even when a wide blur would score zero.
    Nothing to remove is not a reason to risk anything.
    """
    under = [r for r, v in scores.items()
             if v is not None and v <= 100 * budget and r <= cap]
    if under:
        return max(under)
    measured = [r for r, v in scores.items() if v is not None]
    return min(measured) if measured else None


def choose_radius(shots, read_cfa, candidates=RADIUS_CANDIDATES,
                  budget=RADIUS_BUDGET, auto_max=RADIUS_AUTO_MAX):
    """The widest mask blur this bracket can take, and what each one would cost.

    `-r` does not blur the image, it blurs the *layer index*: first a grey
    dilation over a disc of that radius (`fattenMask`, straight out of GIMP),
    then three box passes. Every pixel within the radius of a short-exposure
    island adopts that island's layer outright. Where the short layer has signal
    -- the transition, lit corner to corner -- that costs nothing and removes the
    halo; where it has none -- one bright flank on black, the Baily burst -- the
    same dilation hands the lunar disc to a frame that recorded noise, and the
    noise there went from 3.4 % to 220 %.

    So each candidate is scored by what it would spoil: the share of the frame
    where a pixel some layer measures well (SNR over `USABLE_SNR`) would end up
    served by a layer that does not. The widest one under `budget` wins.
    """
    import numpy as np
    from scipy.ndimage import distance_transform_edt, maximum_filter, uniform_filter

    order = sorted(shots, key=exposure_factor, reverse=True)
    step = 4
    layers = []
    for s in order:
        cfa, black, white = read_cfa(s.path)
        layers.append(((cfa[::step, ::step] - black)
                       / (white - black)).astype(np.float32))
    snr = [np.maximum(x, 0) / READ_NOISE for x in layers]
    unsat = [x < SATURATION for x in layers]

    idx = np.full(layers[0].shape, len(layers) - 1, dtype=np.uint8)
    taken = np.zeros(layers[0].shape, bool)
    for i, x in enumerate(layers[:-1]):
        free = (~taken) & (maximum_filter(x, size=3) < SATURATION)
        idx[free] = i
        taken |= free

    best = np.zeros(idx.shape, np.float32)
    for s, ok in zip(snr, unsat):
        best = np.maximum(best, np.where(ok, s, 0))

    # How far every pixel is from each layer's territory. This does not depend
    # on the radius -- dilating by r is asking which pixels are within r of it --
    # so it is computed once per level instead of once per level per candidate.
    # It used to sit inside the loop, which is what made a longer ladder of
    # candidates look expensive: eight radii cost eight times this, and now they
    # cost it once and a box filter each.
    far = [distance_transform_edt(~(idx >= level))
           for level in range(1, len(layers))]

    scores = {}
    for radius in candidates:
        if radius > auto_max:
            scores[str(radius)] = None      # measured on demand, not chosen here
            continue
        r = max(1, radius // step)
        fat = np.zeros(idx.shape, np.float32)
        for level, d in enumerate(far, start=1):
            fat = np.maximum(fat, np.where(d <= r, float(level), 0.0))
        hr = int(round(r * 0.39))                       # BoxBlur::blur
        for _ in range(3 if hr >= 1 else 0):
            fat = uniform_filter(fat, size=2 * hr + 1, mode="nearest")
        j = np.clip(fat, 0, len(layers) - 1).astype(np.int32)
        served = np.zeros(idx.shape, np.float32)
        for k in range(len(layers)):
            here = j == k
            if here.any():
                served[here] = snr[k][here]
        share = float(((best > USABLE_SNR) & (served < USABLE_SNR)).mean())
        scores[str(radius)] = round(100 * share, 4)

    # Every candidate is scored whatever the cap says, because those numbers
    # are what the window explains the choice with and what a person widening
    # it by hand is trading. The cap decides only what is *chosen*.
    cap = radius_cap(shots)
    numbers = {int(k): v for k, v in scores.items()}
    return auto_radius(numbers, cap) or candidates[0], scores, cap


def bracket_ev_steps(seq):
    """The EV between consecutive steps of a bracket, in the order shot."""
    import math

    steps = []
    for a, b in zip(seq, seq[1:]):
        ea, eb = eff_exposure(a), eff_exposure(b)
        if ea > 0 and eb > 0:
            steps.append(abs(math.log2(eb / ea)))
    return steps


def detect(folder, settings=None, read_cfa=None, report=print, progress=None):
    """Measure a folder and return the plan of what to merge.

    `read_cfa` is injected so a caller can substitute a cache -- the window
    re-detects a single frame far more often than a whole folder -- and so this
    module never imports the develop stack for a job that only reads pixels.
    """
    if read_cfa is None:
        from .develop import read_cfa as read_cfa    # noqa: PLW0127
    settings = dict(settings or {})
    # `blend_radius`: a number to open every frame with it, or "auto" to let
    # the measurement choose per frame. HDRMerge's own default is 3, and that
    # is what a person who has not been told otherwise expects to see.
    want_radius = settings.get("blend_radius")
    want_radius = None if want_radius in (None, "auto") else int(want_radius)
    # Whether to leave out the exposures that resolve nothing. It travels with
    # the radius because it is the same question -- how much of this does the
    # program decide by itself -- and because measured on this material the
    # cost of keeping them is small and one-sided: +1 % noise at radius 3, and
    # a merge anchored ~5 EV lower, which `apply` undoes anyway by reading each
    # merge's scale off its own longest exposure. What it buys is that nothing
    # is missing from a frame you did not look at.
    prune = settings.get("prune_empty", want_radius is None)
    doc = plan.empty(folder, settings)

    ignored = []
    shots = scan(folder, report=report, ignored=ignored)
    if ignored:
        # Kept out of the plan, so out of the list and out of every merge --
        # but said, because a folder that goes in with fourteen files and
        # comes out as two sets has to account for the difference somewhere,
        # and a person who dropped a folder never sees the report the command
        # line prints.
        doc["ignored"] = ignored
        kinds = sorted({i["why"] for i in ignored})
        doc["warnings"].append({
            "what": "ignored_files", "count": len(ignored),
            "says": "%d file%s in the folder %s left out: %s. Only raw frames "
                    "can be merged -- the merge picks, per pixel, which "
                    "exposure to take each sensor site from."
                    % (len(ignored), "" if len(ignored) == 1 else "s",
                       "was" if len(ignored) == 1 else "were",
                       " and ".join(kinds)),
        })
    if not shots:
        raise ValueError(
            "no readable raw files in %s — this merges raw mosaics, so a "
            "folder of developed JPEG or TIFF cannot be used, whatever "
            "metadata it carries" % folder)
    bursts, method = segment(shots, settings.get("gap"))
    kinds, full_n, _single = classify(bursts, settings.get("sync_max_seconds",
                                                          SYNC_MAX_SECONDS))
    doc["segmentation"] = method
    doc["ladder_steps"] = full_n
    doc["detected"] = datetime.datetime.now().replace(microsecond=0).isoformat()
    doc["source_fingerprint"] = plan.fingerprint(folder, RAW_EXTS)

    worst = max((max(bracket_ev_steps(b), default=0) for b in bursts), default=0)
    limit = settings.get("max_ev_step", MAX_BRACKET_EV_STEP)
    if worst > limit:
        doc["warnings"].append({
            "what": "bracket_step", "value_ev": round(worst, 1),
            "limit_ev": limit,
            "says": "The bracket steps by up to %.1f EV, past the %.1f the "
                    "merge wants: dark halos are likely around bright edges. "
                    "This one is the capture's, not the merge's." % (worst, limit),
        })

    min_cov = settings.get("min_step_coverage", MIN_STEP_COVERAGE * 100) / 100.0
    # Both labels are measured over the frames the camera actually shot in
    # this burst, before anything is borrowed: a lent frame comes from another
    # instant and would make a short bracket look complete and a tight one
    # look slow.
    wanted = expected_frames(bursts)
    for i, (burst, kind) in enumerate(zip(bursts, kinds)):
        if progress:
            progress(i, len(bursts))
        if kind == KIND_ORPHAN:
            doc["frames"].append({
                "index": len(doc["frames"]) + 1,
                "anchor": burst[0].name,
                "time": burst[0].time.isoformat(),
                "kind": kind,
                "sync": None,
                "own_frames": len(burst),
                "expected_frames": None,
                "include": False,
                "why": "on its own, and not the setting the other single "
                       "frames use",
                "steps": [{"frame": s.name, "serves_percent": None, "use": False}
                          for s in burst],
                "blend_radius": {"chosen": None, "mode": "auto",
                                 "candidates": {}},
                "borrow": {"used": False},
                "manual": [],
            })
            continue

        merged = list(burst)
        borrowed = {"used": False}
        if kind == KIND_TRUNC and len(burst) >= settings.get("min_own_steps",
                                                            MIN_OWN_STEPS):
            donor, lent, gap = find_donor(bursts, kinds, i)
            if donor is not None:
                merged = list(burst) + list(lent)
                borrowed = {"used": True,
                            "from": donor[0].name,
                            "steps": [s.name for s in lent],
                            "gap_seconds": round(gap, 1)}

        if len(merged) > 1:
            coverage = step_coverage(merged, read_cfa)
            keep, dropped = (useful_steps(merged, coverage, min_cov) if prune
                             else (coverage, []))
            kept = {s.name for s, _ in keep}
            # A fixed radius is not scored. Scoring the ladder means reading
            # every frame again and blurring a mask eight times -- 0.52 s a
            # set, measured -- to answer a question nobody asked: the folder
            # was opened at one radius on purpose. The window can run it on a
            # set, or on a selection, whenever it is wanted, and then it is
            # the person waiting for their own question rather than everybody
            # waiting for one of them.
            if want_radius:
                radius, scores = want_radius, {}
                cap = radius_cap([s for s, _ in keep])
            else:
                radius, scores, cap = choose_radius([s for s, _ in keep],
                                                    read_cfa)
        else:
            coverage, kept, radius, scores, cap = (
                [(merged[0], 1.0)], {merged[0].name}, None, {}, None)

        served = {s.name: round(100 * v, 4) for s, v in coverage}
        doc["frames"].append({
            "index": len(doc["frames"]) + 1,
            "anchor": middle_shot(burst).name,
            "time": middle_shot(burst).time.isoformat(),
            "kind": kind,
            "sync": sync_class(burst),
            "own_frames": len(burst),
            "expected_frames": wanted[i],
            "include": True,
            "steps": [{"frame": s.name,
                       "serves_percent": served.get(s.name),
                       "use": s.name in kept} for s in merged],
            # "fixed" and not "auto": the folder was opened at one radius and
            # that is what every bracket got, whatever the measurement would
            # have said. Recording it as auto made the window claim a number
            # it had not applied -- the row read as measured, and the line
            # under the control explained a choice that was not in force.
            # The cap travels with the scores: the window works out what
            # auto would say without reading a raw again, and it cannot do
            # that from the numbers alone -- the cap is about the bracket's
            # exposure steps, not about its pixels.
            "blend_radius": {"chosen": radius,
                             "mode": "fixed" if want_radius else "auto",
                             "cap": cap,
                             "candidates": scores},
            "borrow": borrowed,
            "manual": [],
        })
    return doc


def add_args(ap):
    """The options of `hdrmerge-timelapser detect`."""
    ap.add_argument("folder", help="The folder of raw frames, one level, no "
                                   "subfolders.")
    ap.add_argument("-o", "--out", metavar="FILE",
                    help="Where to write the plan (default %s beside the "
                         "frames)." % plan.NAME)
    ap.add_argument("--min-step-coverage", type=float,
                    default=MIN_STEP_COVERAGE * 100, metavar="PCT",
                    help="Percent of the frame a bracket step must be the one "
                         "to resolve to be merged at all (default %g).  A step "
                         "nothing needs adds no range and costs noise and a "
                         "shifted scale." % (MIN_STEP_COVERAGE * 100))
    ap.add_argument("--keep-all-exposures", action="store_true",
                    help="Merge every exposure of every bracket, including "
                         "the ones that resolve nothing. On by default when a "
                         "fixed --blend-radius is given: both are the same "
                         "question about who decides.")
    ap.add_argument("--blend-radius", default="auto",
                    help="The mask blur radius every frame opens with: a "
                         "number, or `auto` to let the measurement pick one "
                         "per frame (default auto). HDRMerge's own is 3.")
    ap.add_argument("--max-ev-step", type=float, default=MAX_BRACKET_EV_STEP,
                    metavar="EV",
                    help="Warn when the bracket steps by more than this "
                         "(default %.1f)." % MAX_BRACKET_EV_STEP)
    ap.add_argument("--gap", type=float, metavar="SECONDS",
                    help="Cut brackets where the clock stops instead of by "
                         "the exposure pattern. For a camera that pauses "
                         "between brackets and whose pattern the tool reads "
                         "wrong; useless where brackets are fired back to "
                         "back, which is most timelapses.")
    ap.add_argument("--keep-manual", action="store_true",
                    help="Carry the hand corrections of an existing plan over "
                         "into the new one, matched by anchor frame.")


def run(args):
    """`detect`: measure the folder, write the plan, say what it found."""
    settings = {"blend_radius": args.blend_radius,
                "prune_empty": not args.keep_all_exposures
                and args.blend_radius in (None, "auto"),
                "min_step_coverage": args.min_step_coverage,
                "max_ev_step": args.max_ev_step,
                "gap": args.gap}
    out = args.out or plan.path_for(args.folder)

    def progress(i, n):
        print("\r  measuring bracket %d/%d" % (i + 1, n), end="", file=sys.stderr)

    doc = detect(args.folder, settings, progress=progress)
    print("", file=sys.stderr)

    if args.keep_manual and os.path.isfile(out):
        orphans = plan.carry_over(doc, plan.load(out))
        for o in orphans:
            print("  ! %s was edited by hand and is no longer in the plan (%s)"
                  % (o["anchor"], ", ".join(o["manual"])), file=sys.stderr)

    plan.save(doc, out)
    for w in doc["warnings"]:
        print("  ! %s" % w["says"])
    print("%s  (%s)" % (plan.summary(doc), doc["segmentation"]))
    print("Plan: %s" % out)
    return 0
