#!/usr/bin/env python3
"""Small pictures of raws and merges, made once and kept.

The window is worth having only if it shows the frames, and developing one at
full resolution with the good demosaic takes about nine seconds -- an eternity
in a list you are arrowing through. So a preview is developed decimated and with
the cheap demosaic, which costs under a second and is enough to judge what the
window is for: which frames a merge is made of, whether the limb is clean, what
a different blur radius does to it.

They are cached beside the frames, keyed by the file's size and modification
time, because the second look at a folder should be instant. The cache is
disposable by construction: deleting it costs a second per frame and nothing
else.
"""

import hashlib
import os
import threading

CACHE_DIR = ".hdrmerge-timelapser"
# Decimation for a preview. At 6 a 24 MP frame came out 1011 px wide and the
# window then drew it at 560 -- half of what was already on disk thrown away,
# and nothing left to zoom into. At 3 it is 2022 px for the same 1.1 s of
# developing (the demosaic is the cost; the decimation is what is left after),
# 4.3 MB instead of 1.1, and a limb worth looking at closely. Full resolution
# would be 31 MB and 2.8 s, which is a rendering, not a preview.
STEP = 12                     # decimation for a step's own little picture
LINEAR_STEP = 4               # decimation for the big view, kept in linear
TONEMAP_K = 60.0              # log stretch, so the corona and the limb coexist

# What the cache is allowed to keep. It earns its size -- toggling a step and
# putting it back, or walking a radius up and down, is free the second time --
# but it is disposable by construction, so it is bounded rather than watched:
# whatever has gone longest without being looked at goes first.
CACHE_MAX_BYTES = 3 * 1024 * 1024 * 1024
CACHE_MAX_DAYS = 30


def cache_dir(folder):
    return os.path.join(folder, CACHE_DIR)


def _key(path, tag):
    try:
        st = os.stat(path)
        stamp = "%d-%d" % (st.st_size, int(st.st_mtime))
    except OSError:
        stamp = "gone"
    digest = hashlib.sha1(("%s|%s|%s" % (path, stamp, tag)).encode()).hexdigest()
    return digest[:16] + ".png"


def _base(y, radius, eps=2e-3):
    """The image without its fine structure, with the edges left standing.

    A guided filter, self-guided: four box filters and some arithmetic, which
    on a 1.5 MP preview is milliseconds. A plain Gaussian would be simpler and
    wrong here -- blurring across the lunar limb puts a dark ring around the
    moon when the detail is added back, and a ring around the moon is exactly
    the artefact this window exists to hunt. `eps` is what counts as an edge.
    """
    import numpy as np
    from scipy.ndimage import uniform_filter

    r = max(3, int(radius))
    mean = uniform_filter(y, size=r, mode="nearest")
    mean_sq = uniform_filter(y * y, size=r, mode="nearest")
    var = np.clip(mean_sq - mean * mean, 0, None)
    a = var / (var + eps)
    b = mean - a * mean
    return uniform_filter(a, size=r, mode="nearest") * y + \
        uniform_filter(b, size=r, mode="nearest")


def tonemap(lin, level=None, compress=0.0, detail=0.0, radius=48):
    """Scene-linear to something a screen can show, without lying about shape.

    A log stretch normalised on a high percentile rather than a display curve:
    the subject is a disc with a corona spanning a hundred stops, and a plain
    gamma either buries the corona or blows the limb.

    On top of it, the two knobs that make a corona visible, and both are
    **about the screen and not about the data** -- nothing here reaches the
    EXR, or the plan, or the merge:

    The picture is split into a **base** -- the slow falloff from the limb
    outwards, which is most of the hundred stops and none of the information --
    and the **detail** left over, which is the streamers, the prominences, the
    grain, the seam a bad merge leaves. `compress` flattens the base towards
    its own middle; `detail` amplifies what is left. Squashing the range and
    raising the local contrast are the same gesture done to the two halves, and
    that is why one control could never do it: a global contrast curve moves
    both together, so whatever it reveals at one end it buries at the other.

    This is Durand and Dorsey's decomposition with a guided filter in place of
    the bilateral one, and it is what every published eclipse corona has had
    done to it in one form or another.
    """
    import numpy as np

    if level is None:
        level = float(np.percentile(lin.mean(2), 99.7)) or 1.0
    y = np.log1p(np.clip(lin / level, 0, None) * TONEMAP_K) / np.log1p(TONEMAP_K)
    if compress or detail:
        lum = y.mean(2)
        base = _base(lum, radius)
        fine = lum - base
        anchor = float(np.median(base))
        shown = (anchor + (base - anchor) * (1.0 - 0.85 * compress)
                 + fine * (1.0 + 4.0 * detail))
        # Back to colour by the ratio, so nothing here invents a hue: every
        # channel moves by the same factor its luminance did.
        y = y * (shown / np.maximum(lum, 1e-6))[..., None]
    return (np.clip(y, 0, 1) ** (1 / 1.6) * 255).astype("uint8")


def render_linear(path, dst, cam2out, wb=None, step=LINEAR_STEP, crop=None):
    """Develop one raw or merge and keep it **in scene-linear**, not as a picture.

    The big view used to cache an 8-bit PNG, which is a decision about exposure
    baked into a file: the window could show it and nothing else. Keeping the
    linear data instead costs 5.7 MB a frame at this decimation and buys every
    question worth asking of an HDR merge -- move the exposure and see into the
    shadows, mark what the sensor clipped, count how many stops of real signal
    are there -- all of them instant, because they are arithmetic on an array
    that is already in memory.

    Half floats, zlib: 0.04 s to write and 0.01 s to read back. The sensor's
    limits ride along in the description, because absolute radiance rescales
    everything and they cannot be recovered from the numbers afterwards.
    """
    import json

    import numpy as np
    import tifffile

    from . import develop as dev

    lin, levels = dev.develop(path, cam2out, 0.0, wb_source=None,
                              wb_override=wb, demosaic="bilinear", crop=crop,
                              with_levels=True)
    small = np.ascontiguousarray(lin[::step, ::step])
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = "%s.%d.%d.part" % (dst, os.getpid(), threading.get_ident())
    tifffile.imwrite(tmp, small.astype("float16"), compression="zlib",
                     description=json.dumps(levels))
    os.replace(tmp, dst)
    return dst


def mask_path(linear):
    """Where the layer map of a merge lives, beside its linear preview."""
    return linear[:-4] + "-mask.png"


def load_mask(linear):
    """Which layer HDRMerge took each pixel from, decimated like the preview.

    None when the picture is not a merge, or was made before this was kept.
    """
    import numpy as np
    from PIL import Image

    path = mask_path(linear)
    if not os.path.isfile(path):
        return None
    return np.array(Image.open(path))


def load_linear(path):
    """The linear preview and the sensor's limits in its units."""
    import json

    import tifffile

    with tifffile.TiffFile(path) as tf:
        lin = tf.asarray().astype("float32")
        try:
            levels = json.loads(tf.pages[0].description or "{}")
        except ValueError:
            levels = {}
    return lin, levels


def render(path, dst, cam2out, wb=None, step=STEP, crop=None, level=None,
           width=None):
    """Develop one raw or merge into a small PNG. Returns the path written.

    A crop is taken before the demosaic, which is what makes it cheap: the same
    detail cost 2.1 s developed whole and cropped afterwards, and 0.2 s this way.
    The tone level is measured on the crop then, so a detail of the limb is
    exposed for the limb rather than for a frame it can no longer see.
    """
    import numpy as np
    from PIL import Image

    from . import develop as dev

    lin = dev.develop(path, cam2out, 0.0, wb_source=None, wb_override=wb,
                      demosaic="bilinear", crop=crop)
    full = float(np.percentile(lin.mean(2), 99.7)) or 1.0
    if crop:
        step = 1
    img = Image.fromarray(tonemap(lin[::step, ::step], level or full))
    if width and img.width > width:
        img = img.resize((width, round(width * img.height / img.width)),
                         Image.LANCZOS)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    # Through a temporary and renamed on success: two previews of the same file
    # can be asked for at once -- the list and the big view -- and a half-written
    # PNG read by the other one comes back as a CRC error and a black frame. The
    # temporary is unique per *thread* and not per process: the previews run in a
    # pool inside one process, so naming it after the pid gave both of them the
    # same path, and whichever renamed second found its own file gone.
    tmp = "%s.%d.%d.part" % (dst, os.getpid(), threading.get_ident())
    img.save(tmp, format="PNG")
    os.replace(tmp, dst)
    return dst, full


def touch(path):
    """Mark a cached file as used, so pruning takes the ones nobody looks at."""
    try:
        os.utime(path, None)
    except OSError:
        pass
    return path


def prune(folder, max_bytes=CACHE_MAX_BYTES, max_days=CACHE_MAX_DAYS):
    """Keep the cache under a size and an age. Returns (files, bytes) removed.

    Least recently used first, by modification time, which every cache hit
    refreshes. Deleting any of this costs a re-merge and nothing else, so the
    policy can afford to be blunt.
    """
    import time

    d = cache_dir(folder)
    if not os.path.isdir(d):
        return 0, 0
    files = []
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    files.sort()
    total = sum(f[1] for f in files)
    old = time.time() - max_days * 86400
    gone = bytes_gone = 0
    for mtime, size, p in files:
        if total <= max_bytes and mtime >= old:
            break                      # sorted: everything after is newer too
        try:
            os.remove(p)
        except OSError:
            continue
        total -= size
        gone += 1
        bytes_gone += size
    return gone, bytes_gone


def view_path(folder, path):
    """Where one source frame's linear preview lives, made or not."""
    return os.path.join(cache_dir(folder),
                        _key(path, "view-%s" % LINEAR_STEP)).replace(".png",
                                                                     ".tif")


def view_cached(folder, path):
    """That preview if it has already been made, else None."""
    dst = view_path(folder, path)
    return touch(dst) if os.path.isfile(dst) else None


def view(folder, path, cam2out, wb=None):
    """The cached linear preview of one source frame, for the big view."""
    dst = view_path(folder, path)
    if os.path.isfile(dst):
        return touch(dst)
    return render_linear(path, dst, cam2out, wb=wb)


def clear(folder):
    """Delete a folder's whole preview cache. Returns (files, bytes) removed.

    Everything in there can be made again from the raws and the plan, so this
    is a slow undo and never a loss -- which is exactly why it is offered:
    when something looks wrong and you want to know whether it is the cache
    lying or the merge, the cheap experiment is to throw the cache away.
    """
    d = cache_dir(folder)
    if not os.path.isdir(d):
        return 0, 0
    gone = bytes_gone = 0
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            size = os.path.getsize(p)
            os.remove(p)
        except OSError:
            continue
        gone += 1
        bytes_gone += size
    try:
        os.rmdir(d)
    except OSError:
        pass
    return gone, bytes_gone


def thumb(folder, path, cam2out, wb=None, step=STEP, crop=None, tag="",
          width=None):
    """The cached picture of a file, developing it the first time only."""
    dst = os.path.join(cache_dir(folder),
                       _key(path, "%s-%s-%s-%s" % (step, crop, tag, width)))
    if os.path.isfile(dst):
        return touch(dst)
    return render(path, dst, cam2out, wb=wb, step=step, crop=crop,
                  width=width)[0]


def colour(folder, sample):
    """The camera-to-output matrix for a folder, read from one of its raws.

    From the *original* raw and never from a merge: HDRMerge relabels the colour
    tags -- the Sigma fp dual-illuminant bug -- and a preview developed through
    the wrong matrix is green, which would send somebody hunting a problem that
    is not there.
    """
    from . import develop as dev

    return dev.cam_to_output(dev.xyz2cam_from_raw(sample), dev.XYZ2P3D60)


def merge_sources(folder, frame, sources, whole=True):
    """The files to hand HDRMerge: the raws, shifted where the plan says.

    A step with an offset is written out once, moved, into the cache, and it is
    that copy that gets merged. The original is never touched -- this program
    reads raws and does not write them.

    `whole` is about the frame's own offset, the one that moves every step
    together. Translation commutes with the merge -- moving all the inputs by
    the same amount is moving the result -- so the **window** does not need it
    here: it shifts the picture it already has, which is instant, and passes
    `whole=False`. `apply` passes it on, because there the shift has to be in
    the pixels. Preserving that distinction is the difference between nudging
    a frame at the speed of the arrow key and at ten seconds a press.
    """
    from . import align, plan

    # The frame's own offset moves every step by the same amount -- where the
    # whole merge sits -- and each step's moves it against the others. They add
    # because they answer different questions and neither replaces the other.
    slide = tuple(frame.get("offset") or (0, 0)) if whole else (0, 0)
    offsets = {s["frame"]: tuple(s.get("offset") or (0, 0))
               for s in frame["steps"]}
    out = []
    for name in plan.merged_steps(frame):
        path = sources[name]
        own = offsets.get(name, (0, 0))
        dx, dy = own[0] + slide[0], own[1] + slide[1]
        if dx or dy:
            moved = align.shifted_source(cache_dir(folder), path, dx, dy)
            if moved:
                path = moved
        out.append(path)
    return out


def merge_path(folder, frame, radius, crop=None, width=None):
    """Where the picture of this exact merge lives, made or not.

    The key is the steps **and what those files currently are** -- name, size
    and modification time -- plus the radius, the crop and the width. Putting a
    step back therefore returns the picture made before it came out, without
    merging again, while a source that has been rewritten does not.

    That last part is not hypothetical here: the frames this reads are usually
    the output of `eclipse-aligner`, which re-exports the same file names with
    different pixels every time somebody fixes a registration by hand. Keyed on
    names alone, the window would have gone on showing merges of the version
    before the fix, and nothing on screen would have said so.
    """
    from . import plan

    # The frame's own offset is deliberately **not** in the key: the picture it
    # names is the same picture moved, and the window moves it when it draws.
    # Keyed on it, every nudge of the whole frame threw away a merge that was
    # still perfectly good and made it again.
    offsets = {s["frame"]: tuple(s.get("offset") or (0, 0))
               for s in frame["steps"]}
    stamp = []
    for name in plan.merged_steps(frame):
        try:
            st = os.stat(os.path.join(folder, name))
            stamp.append("%s:%d:%d" % (name, st.st_size, int(st.st_mtime)))
        except OSError:
            stamp.append("%s:gone" % name)
        dx, dy = offsets.get(name, (0, 0))
        if dx or dy:
            stamp[-1] += ":%+.2f%+.2f" % (dx, dy)
    return os.path.join(cache_dir(folder), _key(
        frame["anchor"], "merge-%s-%s-%s-%s%s"
        % (radius, crop, width, "|".join(stamp), ""))
        ).replace(".png", ".tif")


def cached_merge(folder, frame, radius, crop=None, width=None):
    """The picture of this merge if it has already been made, else None."""
    p = merge_path(folder, frame, radius, crop=crop, width=width)
    return touch(p) if os.path.isfile(p) else None


def hidden_kwargs():
    """What to pass Popen so a child process never shows a window.

    Windows only, and it is a different problem from the Dock icon. The
    HDRMerge that ships for Windows is `hdrmerge.com`, the *console* build --
    that is how it has any command-line output at all -- and a console
    program started from a program that has a window is given a console
    window of its own. So every merge opens a black box, and a folder of them
    flashes one per frame. CREATE_NO_WINDOW is the documented way to say the
    child needs no console; its output still arrives, because it arrives
    through the pipes we already ask for.

    It applies to every child, not only HDRMerge: exiftool and the `apply`
    subprocess the window starts would each flash their own.

    Empty everywhere else, where a console is not something a child is handed.
    """
    if os.name != "nt":
        return {}
    import subprocess

    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def run_hdrmerge(cmd):
    """Run a merge without HDRMerge arriving in the Dock.

    HDRMerge is a Qt application even when it is doing a command line's work,
    and a Qt application on macOS asks the window server to make it a
    foreground app: an icon in the Dock, and the focus taken off whatever the
    person was reading. Once is a blink; a folder of twenty merges, plus one
    per preview while somebody browses the list, is a strobe.

    `QT_QPA_PLATFORM=offscreen` is the whole fix -- Qt loads a platform plugin
    that never talks to the window server. Measured on a three-frame merge:
    with it, nothing registers with Launch Services; the merged CFA is
    identical to the byte (4042x6064 float32, max difference 0.0), and the
    only difference in the file is the DateTime tag, which is the clock.

    The plugin ships inside our own app (`plugins/platforms/libqoffscreen.dylib`
    next to the bundled HDRMerge), but somebody else's build may not have it,
    and a merge that will not run is a worse trade than an icon that blinks.
    So a failure that names the platform plugin is retried as it was before.
    """
    import subprocess

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    done = subprocess.run(cmd, capture_output=True, env=env, **hidden_kwargs())
    if done.returncode != 0 and b"platform plugin" in (done.stderr or b"").lower():
        done = subprocess.run(cmd, capture_output=True,   # the icon, but it runs
                              **hidden_kwargs())
    return done


def merge_preview(folder, frame, sources, radius, hdrmerge, cam2out, wb=None,
                  crop=None, width=None):
    """Merge one frame as the plan currently stands and return a picture of it.

    The cache key carries **which steps are in the merge**, not only the radius.
    Keying it on the frame and the radius alone meant that taking a step out and
    asking again returned the picture of the merge that still had it -- the
    window answering a question with the answer to the previous one, which is
    the one bug a window like this must not have.
    """
    import subprocess

    from . import plan

    steps = plan.merged_steps(frame)
    os.makedirs(cache_dir(folder), exist_ok=True)
    picture = merge_path(folder, frame, radius, crop=crop, width=width)
    if os.path.isfile(picture):
        return touch(picture)

    # The intermediate DNG is named per thread, not after the picture: two
    # merges asked for at once can arrive at the same key -- edit a step and put
    # it straight back -- and with a shared name the one that finished first
    # deleted the file the other was still developing.
    merged = "%s.%d.%d.dng" % (picture[:-4], os.getpid(), threading.get_ident())
    # `-m` costs nothing -- HDRMerge has the mask in hand and writes it as an
    # indexed PNG, 50 KB for a 24 MP frame -- and it is the one thing about a
    # merge that cannot be worked out from the result: which exposure each
    # pixel actually came from.
    raw_mask = merged[:-4] + "-mask.png"
    cmd = [hdrmerge, "-b", "32", "-o", merged, "--no-crop",
           "-r", str(radius), "-m", raw_mask]
    cmd += ["--no-align"]
    cmd += merge_sources(folder, frame, sources, whole=False)
    done = run_hdrmerge(cmd)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or b"").decode()[-200:]
                           or "HDRMerge returned %d" % done.returncode)
    if not os.path.isfile(merged):
        raise RuntimeError("HDRMerge wrote nothing for %d steps at r%s"
                           % (len(steps), radius))
    try:
        render_linear(merged, picture, cam2out, wb=wb, crop=crop)
        _keep_mask(raw_mask, mask_path(picture), crop)
    finally:
        for leftover in (merged, raw_mask):
            if os.path.exists(leftover):
                os.remove(leftover)
    return picture


def _keep_mask(src, dst, crop=None, step=LINEAR_STEP):
    """Decimate HDRMerge's mask exactly as the preview was, and keep that.

    Same slicing as `render_linear`, so the two line up pixel for pixel and the
    window can colour the picture by layer without resampling anything. The
    full-size mask is 24 MP of layer indices; this is a few tens of kilobytes.
    """
    import numpy as np
    from PIL import Image

    if not os.path.isfile(src):
        return None
    m = np.array(Image.open(src))
    if crop:
        top, bottom, left, right = (v - v % 2 for v in crop)
        m = m[top:bottom, left:right]
    Image.fromarray(m[::step, ::step].astype("uint8")).save(dst)
    return dst
