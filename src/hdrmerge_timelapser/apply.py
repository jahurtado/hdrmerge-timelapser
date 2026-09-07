#!/usr/bin/env python3
"""Execute a merge plan: call HDRMerge for every frame, then develop to EXR.

This command decides nothing. Whatever the plan says -- these steps, that
radius, this frame left out -- is what happens, because every judgement was
made in `detect` and every correction in the window. Keeping it that way is what
makes a long run repeatable: the document is the record of what was asked for,
and the manifest beside the output is the record of what came out.
"""

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from . import plan
from . import preview
from .detect import RAW_EXTS, exposure_factor, read_shot

# Where the processed sequence goes unless told otherwise: a folder beside the
# raws, named for what is in it. Inside the source folder rather than next to
# it, so a project stays one directory you can move, archive or delete whole.
OUT_DIR = "merged"

WINDOWS_HDRMERGE = r"C:\Program Files\HDRMerge\hdrmerge.com"
MERGE_RAM_GB = 6.0            # Menon peaks about here on a 24 MP frame


def load_dotenv(path=".env"):
    """The few settings a person keeps out of the repo, HDRMERGE_BIN mostly."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))
    except OSError:
        pass


def bundled_hdrmerge():
    """The copy of HDRMerge that ships inside the app, if this is the app.

    A packaged build carries its own -- HDRMerge is the merge, and a program
    that asks the person to install the thing it is made of is not packaged. It
    is looked for first, so the bundle never depends on what happens to be on
    the machine, and the machine's copy still wins when it is named explicitly.

    Two bundle shapes, because the two platforms freeze differently. The macOS
    app is a PyInstaller build: its files sit under `sys._MEIPASS`. The Windows
    app is an embeddable CPython -- no `_MEIPASS` at all -- laid out as
    `<payload>\\python\\pythonw.exe` with HDRMerge copied to
    `<payload>\\hdrmerge\\`, one level up from the interpreter's own folder. Run
    from source, neither path exists, so this returns None and the machine's
    HDRMerge is used.
    """
    root = getattr(sys, "_MEIPASS", None)
    if root:
        here = os.path.join(root, "hdrmerge", "hdrmerge")
        return here if os.path.isfile(here) else None
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.join(os.path.dirname(exe_dir), "hdrmerge", "hdrmerge.com")
    return here if os.path.isfile(here) else None


def resolve_hdrmerge(explicit=None):
    """Where HDRMerge is: the flag, the bundle, $HDRMERGE_BIN, PATH, Windows."""
    load_dotenv()
    for candidate in (explicit, bundled_hdrmerge(),
                      os.environ.get("HDRMERGE_BIN"),
                      shutil.which("hdrmerge"), WINDOWS_HDRMERGE):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return candidate
    raise SystemExit(
        "HDRMerge not found. Pass --hdrmerge PATH, set HDRMERGE_BIN in .env, "
        "or put `hdrmerge` on the PATH. See docs/installation.md.")


def develop_jobs(demosaic, jobs):
    """How many develops fit in RAM at once.

    Menon peaks around 6 GB on a 24 MP frame, so running one per core is how a
    machine with plenty of cores ends up swapping: eleven of them at 6 GB asked
    for more than 60 GB. The cheaper demosaics do not need the cap.
    """
    if demosaic != "menon":
        return jobs
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        return max(1, min(jobs, 4))
    return max(1, min(jobs, int(total * 0.7 / MERGE_RAM_GB)))


def command(hdrmerge, frame, sources, out_file, bps=32, align=False,
            folder=None):
    """The HDRMerge invocation one frame of the plan turns into.

    HDRMerge's own alignment stays off. It registers the frames of a bracket
    against each other and gives no number back and no way to correct it, and
    the offset it finds differs from burst to burst -- ~46 px between
    consecutive ones on a tripod -- so the framing wanders across the
    timelapse. The editor lines the steps up itself instead, in measured
    pixels that live in the plan.

    `--no-crop` stays either way: cropping to the aligned overlap would give a
    different size per frame, and a sequence cannot have that.
    """
    cmd = [hdrmerge, "-b", str(bps), "-o", out_file, "--no-crop"]
    if not align:
        cmd += ["--no-align"]
    radius = (frame.get("blend_radius") or {}).get("chosen")
    if radius:
        cmd += ["-r", str(radius)]
    cmd += preview.merge_sources(folder, frame, sources)
    return cmd


def _read_sources(folder):
    """Every raw in the folder, read, or a sentence about the one that would not.

    `detect` measured these files and `apply` reads them again, and between
    the two a file can move, a volume can go away, a symlink can be left
    pointing at nothing. Unhandled, the first such file ends the run with a
    stack trace from whichever library got to it -- which in a window is
    "something failed" and a number. Named, it is a thing a person can go and
    fix.
    """
    for name in sorted(os.listdir(folder)):
        if os.path.splitext(name)[1].lower() not in RAW_EXTS:
            continue
        path = os.path.join(folder, name)
        try:
            yield read_shot(path)
        except Exception as e:                              # noqa: BLE001
            # A dangling symlink is the common one, and every reader reports
            # it as its own kind of I/O error -- LibRaw says `b'Input/output
            # error'`, which tells nobody anything. Ask the filesystem first.
            why = ("the file it points to is gone"
                   if os.path.islink(path) and not os.path.exists(path)
                   else "it is not there" if not os.path.exists(path)
                   else str(e).strip("b'\""))
            raise ValueError(
                "%s is in the plan but cannot be read now: %s. The folder has "
                "changed since it was measured." % (name, why))


def _merge(task):
    """Run one merge and say whether it produced a file, not whether it ran.

    HDRMerge **exits 0 when it has loaded nothing**: hand it a path that does
    not exist, or an image that is not raw, and it prints "Error loading …"
    and returns success having written no DNG. Trusting the exit code wrote
    `"status": "ok"` into the manifest for a frame that was never merged, and
    left the failure to surface further down as a develop that cannot find
    its input -- an error about the wrong step.

    So the test is the file: written, and not empty.
    """
    cmd, rec = task
    out_file = cmd[cmd.index("-o") + 1]
    r = preview.run_hdrmerge(cmd)
    wrote = os.path.isfile(out_file) and os.path.getsize(out_file) > 0
    rec["status"] = "ok" if r.returncode == 0 and wrote else "failed"
    rec["exit_code"] = r.returncode
    if rec["status"] == "failed":
        rec["stderr"] = (r.stderr.decode("utf-8", "replace")[-400:]
                         or "HDRMerge wrote no file and reported no error")
    return rec


def _develop(task):
    (src, out_file, cam2out, exposure_ev, bits, wb_source, wb, demosaic,
     highlight, scale, tone) = task
    from . import develop as dev
    lin = dev.develop(src, cam2out, exposure_ev, wb_source=wb_source,
                      wb_override=wb, demosaic=demosaic, highlight=highlight,
                      exposure_override=scale, clamp=bool(tone))
    if tone:
        dev.write_tiff(lin, out_file, bits, tone["curve"], tone.get("level"))
    else:
        dev.write_exr(lin, out_file, bits)
    return out_file


def reference_level(dev_tasks, report=print, frames=5):
    """The white an integer TIFF is written against, measured from the run.

    An integer file has to be told where the top is, and the number has to be
    the same for every frame or the sequence flickers. It used to come from
    the window -- the 99.7th percentile of whichever frame was on screen when
    Process was pressed -- so the brightness of a whole sequence depended on
    where somebody had left the list. It is measured here instead, from the
    material and nothing else.

    A handful of frames spread across the run, developed with the cheap
    demosaic since a percentile does not need a good one, and the **highest**
    of their references wins: sized to the brightest thing in the sequence,
    nothing clips, and the frames below it keep their relative brightness.
    """
    import numpy as np

    from . import develop as dev

    picks = [dev_tasks[i] for i in
             sorted({round(i * (len(dev_tasks) - 1) / max(frames - 1, 1))
                     for i in range(min(frames, len(dev_tasks)))})]
    report("Measuring the reference on %d of %d frames" % (len(picks),
                                                           len(dev_tasks)))
    tops = []
    for (src, _out, cam2out, ev, _bits, wb_source, wb, _demosaic,
         highlight, scale, _tone) in picks:
        lin = dev.develop(src, cam2out, ev, wb_source=wb_source, wb_override=wb,
                          demosaic="bilinear", highlight=highlight,
                          exposure_override=scale, clamp=True)
        tops.append(float(np.percentile(lin, 99.7)))
    return max(tops) or 1.0


def measured_scale(merged_path, sources, read_cfa, sample=4):
    """The exposure a merged frame must be divided by to read as radiance.

    HDRMerge anchors a merge on the shortest layer it was given, but not exactly
    on that layer's exposure ratio, and the anchor moves whenever the merge is
    given a different set of steps -- which per-frame pruning does. So the scale
    is read off the pixels: the merge is compared with the longest source of its
    own bracket, a real exposure of known length, over the pixels where that
    source is neither in the noise nor blown. Measured this way, an ISO 100 to
    640 change across a sequence costs 0.15 EV instead of 2.7.
    """
    import numpy as np

    longest = max(sources, key=exposure_factor)
    m_cfa, m_black, m_white = read_cfa(merged_path)
    l_cfa, l_black, l_white = read_cfa(longest.path)
    merged = (m_cfa[::sample, ::sample] - m_black) / (m_white - m_black)
    source = (l_cfa[::sample, ::sample] - l_black) / (l_white - l_black)
    usable = (source > 0.02) & (source < 0.9)
    if usable.sum() < 1000:
        return None
    ratio = float(np.median(merged[usable] / source[usable]))
    return ratio * exposure_factor(longest) if ratio > 0 else None


def apply(doc, out_dir, options, report=print, progress=None):
    """Do what the plan says. Returns the manifest of what was done."""
    from . import develop as dev

    # Before a single merge runs: an hour of work that ends in a complaint
    # about an argument is an hour the complaint could have been made in.
    if options.get("format", "exr") == "exr" and options.get("exr_bits") == 8:
        raise ValueError("an EXR is written at 16 or 32 bits; 8 is for a TIFF")

    hdrmerge = resolve_hdrmerge(options.get("hdrmerge"))
    folder = doc["source_dir"]
    shots = {s.name: s for s in _read_sources(folder) if s}
    paths = {name: s.path for name, s in shots.items()}

    dng_dir = os.path.join(out_dir, "dng")
    os.makedirs(dng_dir, exist_ok=True)
    frames = [f for f in doc["frames"] if f["include"]]
    manifest = {"plan": doc.get("detected"), "source_dir": folder,
                "output_dir": os.path.abspath(out_dir),
                "settings": doc["settings"], "frames": []}

    tasks = []
    for f in frames:
        rec = {"index": f["index"], "anchor": f["anchor"], "kind": f["kind"],
               "sources": [s["frame"] for s in f["steps"]],
               "merged_from": plan.merged_steps(f),
               "blend_radius": (f.get("blend_radius") or {}).get("chosen")}
        if len(rec["merged_from"]) > 1:
            out_file = os.path.join(dng_dir,
                                    os.path.splitext(f["anchor"])[0] + ".dng")
            rec["dng"] = os.path.relpath(out_file, out_dir)
            tasks.append((command(hdrmerge, f, paths, out_file,
                                  options.get("bps", 32),
                                  options.get("align", False),
                                  folder=folder), rec))
        else:
            rec["status"] = "passthrough"
        manifest["frames"].append(rec)

    jobs = options.get("jobs") or max(1, (os.cpu_count() or 2) - 1)
    if tasks:
        report("Merging %d sets (%d parallel)" % (len(tasks), jobs))
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for i, rec in enumerate(ex.map(_merge, tasks), 1):
                if progress:
                    progress("merge", i, len(tasks))
                if rec["status"] != "ok":
                    report("  ! %s: hdrmerge exit %s"
                           % (rec.get("dng"), rec.get("exit_code")))

    if options.get("no_exr"):
        return manifest

    space = options.get("exr_colorspace", "rec709")
    cam_raw = options.get("cam_raw") or next(iter(paths.values()))
    cam2out = dev.cam_to_output(dev.xyz2cam_from_raw(cam_raw),
                                dev.OUTPUT_SPACES.get(space, dev.XYZ2REC709))
    demosaic = options.get("exr_demosaic", "menon")
    # Named after the folder the frames came from, not after the folder they
    # are going into: `clips_00001.exr` says nothing, and every project would
    # produce the same name.
    prefix = (options.get("exr_name")
              or os.path.basename(folder.rstrip(os.sep))
              or os.path.basename(out_dir.rstrip(os.sep)))

    # What comes out is **the whole timelapse**, in capture order, with each
    # bracket replaced by its merge. The frames that had nothing to merge are
    # copied through by default rather than dropped: a sequence with holes in
    # it is not a timelapse, and re-developing a single exposure that nobody
    # asked to change is work spent to lose its own format.
    fmt = options.get("format", "exr")
    keep_singles = options.get("include_unmerged", True)
    convert_singles = options.get("convert_unmerged", False)
    # For an integer format there is a curve to choose and a reference to fix;
    # for EXR there is neither, and `tone` stays None.
    tone = ({"curve": options.get("tiff_curve", "srgb"),
             "level": options.get("tone_level")}
            if fmt == "tiff" else None)

    dev_tasks, dev_recs, copies, number = [], [], [], 0
    for rec in manifest["frames"]:
        f = next(x for x in frames if x["index"] == rec["index"])
        merged = bool(rec.get("dng"))
        if merged and rec.get("status") != "ok":
            continue
        if not merged and not keep_singles:
            continue
        number += 1
        if merged:
            src = os.path.join(out_dir, rec["dng"])
            scale = measured_scale(src, [shots[n] for n in rec["merged_from"]],
                                   dev.read_cfa)
            rec["measured_scale"] = scale and float("%.6g" % scale)
        else:
            src = paths[f["anchor"]]
            scale = None
        develop_it = fmt in ("exr", "tiff") and (merged or convert_singles)
        if fmt in ("dng14", "dng16") and not merged and convert_singles:
            develop_it = False
        if develop_it:
            out_file = os.path.join(out_dir, "%s_%05d.%s"
                                    % (prefix, number,
                                       "exr" if fmt == "exr" else "tif"))
            dev_tasks.append((src, out_file, cam2out,
                              options.get("exr_exposure", 0.0),
                              options.get("exr_bits", 32), paths[f["anchor"]],
                              options.get("exr_wb"), demosaic,
                              options.get("exr_highlight", "neutral"), scale,
                              tone))
            dev_recs.append(rec)
        elif fmt in ("dng14", "dng16") and merged:
            # The merge as an ordinary integer DNG: quantised linearly over
            # its own black-to-white range, which is the file every raw editor
            # opens without an argument. 14 bits is the one Resolve wants.
            out_dng = os.path.join(out_dir, "%s_%05d.dng" % (prefix, number))
            dev.write_linear_dng(src, out_dng, paths[f["anchor"]],
                                 dev.read_cfa,
                                 bits=14 if fmt == "dng14" else 16)
            rec["written"] = os.path.basename(out_dng)
        else:
            # Copied, keeping the extension it came with: this is the branch
            # that leaves an untouched frame untouched.
            out_raw = os.path.join(out_dir, "%s_%05d%s"
                                   % (prefix, number,
                                      os.path.splitext(src)[1].lower()))
            copies.append((src, out_raw))
            rec["copied"] = os.path.basename(out_raw)

    for src, dst in copies:
        shutil.copy2(src, dst)
    if copies:
        report("Copied %d frames through, unchanged" % len(copies))

    if dev_tasks and tone and not tone.get("level"):
        tone["level"] = reference_level(dev_tasks, report)
        manifest["tone_level"] = float("%.6g" % tone["level"])
        for i, task in enumerate(dev_tasks):
            dev_tasks[i] = task[:-1] + (dict(tone),)

    if dev_tasks:
        ejobs = develop_jobs(demosaic, jobs)
        report("Developing %d %s (%s, %s) (%d parallel)"
               % (len(dev_tasks),
                  "EXR" if fmt == "exr" else "%d-bit TIFF"
                  % options.get("exr_bits", 16),
                  "linear " + space if not tone or tone["curve"] == "linear"
                  else "sRGB", demosaic, ejobs))
        # Threads, not processes, and on macOS that is the difference between
        # one icon in the Dock and one per worker. A frozen build starts a
        # process-pool worker by re-executing its own binary, and a binary
        # inside a .app is that app: each worker checked in with the bundle's
        # identity as a Foreground application, so a folder of merges filled
        # the Dock with copies of the program.
        #
        # It costs nothing to give them up. Measured on six 24 MP frames, four
        # at a time: 27.9 s against 32.5 s -- threads are *faster*, having no
        # interpreters to start and no 24-megapixel arrays to pickle across --
        # the six EXRs come out identical to the byte, and the peak memory of
        # the whole tree is the same, 15.8 GB against 15.6 GB. The develop is
        # numpy and scipy from end to end, and both let the GIL go.
        with ThreadPoolExecutor(max_workers=ejobs) as ex:
            for i, out_exr in enumerate(ex.map(_develop, dev_tasks), 1):
                dev_recs[i - 1]["exr"] = os.path.basename(out_exr)
                if progress:
                    progress("develop", i, len(dev_tasks))
    return manifest


def add_args(ap):
    """The options of `hdrmerge-timelapser apply`."""
    ap.add_argument("plan", nargs="?", help="The plan, or the folder holding "
                                            "it (default: this folder's).")
    ap.add_argument("-o", "--out", metavar="DIR",
                    help="Where the EXR sequence and merged DNG go "
                         "(default <folder>/merged, beside the raws it came from).")
    ap.add_argument("--hdrmerge", metavar="PATH", help="The HDRMerge binary.")
    ap.add_argument("-b", "--bps", type=int, default=32, choices=(16, 24, 32),
                    help="Bits per sample of the merged float DNG (default 32; "
                         "16 is half-float and posterizes deep shadows).")
    ap.add_argument("-j", "--jobs", type=int, help="Parallel merges "
                                                   "(default: CPU count - 1).")
    ap.add_argument("--no-exr", action="store_true",
                    help="Merge only; skip the develop.")
    ap.add_argument("--exr-wb", type=float, nargs=3, metavar=("R", "G", "B"),
                    help="Fixed white balance. Auto WB drifts frame to frame "
                         "and the sequence flickers in colour.")
    ap.add_argument("--exr-bits", type=int, default=32, choices=(8, 16, 32),
                    help="Bits per sample of the developed file: 16 or 32 for "
                         "an EXR, and 8 as well for a TIFF, where it is a "
                         "proof rather than data. The window has offered an "
                         "8-bit TIFF since the beginning and this only "
                         "accepted 16 or 32, so choosing it failed with "
                         "argparse's exit 2 and the window said `something "
                         "failed`.")
    ap.add_argument("--exr-colorspace", default="rec709",
                    choices=("rec709", "p3-d65", "p3-d60", "rec2020"),
                    help="Output primaries (default rec709, which shares "
                         "them with sRGB). Measured on this material, nothing "
                         "falls outside it; p3-d60 is there for a wide-gamut "
                         "pipeline that wants it anyway.")
    ap.add_argument("--exr-demosaic", default="menon",
                    choices=("bilinear", "malvar", "menon"))
    ap.add_argument("--exr-exposure", type=float, default=0.0, metavar="EV")
    ap.add_argument("--tiff-curve", default="srgb",
                    choices=("linear", "srgb"),
                    help="What an integer TIFF carries: the scene-linear "
                         "numbers quantised, or the picture with a display "
                         "curve on it.")
    ap.add_argument("--tone-level", type=float,
                    help="The value that becomes white, fixed for the whole "
                         "run. Measured per frame it would be flicker.")
    ap.add_argument("--format", default="exr",
                    choices=("exr", "dng", "dng14", "dng16", "tiff"),
                    help="What the merged frames come out as (default exr). "
                         "`dng` keeps HDRMerge's float DNG and skips the "
                         "develop.")
    ap.add_argument("--no-unmerged", action="store_true",
                    help="Leave the frames that have no bracket out of the "
                         "output. By default they are written too, so the "
                         "sequence is the whole timelapse.")
    ap.add_argument("--convert-unmerged", action="store_true",
                    help="Develop those frames to the output format as well. "
                         "Off by default: they are copied as they are, in the "
                         "format they came in.")


def leave_the_dock():
    """Give up this process's place in the Dock. macOS, and only when frozen.

    The window runs the work as a subprocess so that Stop can stop it, and in
    a build that subprocess is the same binary inside the same .app -- so
    macOS registers it as a second copy of the program, and the person
    watching a progress bar sees two identical icons in the Dock, one of them
    inert.

    `TransformProcessType` is how a running process says it is not that kind
    of program. It is asked for `kProcessTransformToBackgroundApplication`,
    which drops the Dock tile and the menu bar and changes nothing else: this
    process has no window and wants none.

    Wrapped in a try, and silent: an icon too many is a blemish, and refusing
    to merge a folder over a blemish would be the worse bug.
    """
    import sys

    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        import ctypes.util

        class PSN(ctypes.Structure):
            _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]

        lib = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("ApplicationServices"))
        current, background = PSN(0, 2), 2      # kCurrentProcess, to background
        lib.TransformProcessType(ctypes.byref(current), ctypes.c_uint32(background))
    except Exception:                                       # noqa: BLE001
        pass


def run(args):
    """`apply`: read the plan and do it."""
    import json

    leave_the_dock()

    target = args.plan or "."
    path = target if os.path.isfile(target) else plan.path_for(target)
    if not os.path.isfile(path):
        print("No plan at %s -- run `detect` first." % path, file=sys.stderr)
        return 1
    doc = plan.load(path)
    out_dir = args.out or os.path.join(doc["source_dir"], OUT_DIR)
    options = {"include_unmerged": not args.no_unmerged,
               "convert_unmerged": args.convert_unmerged, "format": args.format}
    options.update({k: getattr(args, k) for k in
                    ("hdrmerge", "bps", "jobs", "no_exr", "exr_wb", "exr_bits",
                     "exr_colorspace", "exr_demosaic", "exr_exposure",
                     "tiff_curve", "tone_level")})
    options.update(doc["settings"].get("apply", {}))

    def progress(stage, i, n):
        print("\r  %s %d/%d" % (stage, i, n), end="", file=sys.stderr)

    manifest = apply(doc, out_dir, options, progress=progress)
    print("", file=sys.stderr)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)
    failed = [f for f in manifest["frames"] if f.get("status") == "failed"]
    print("Done. %d frames, %d merges failed. Output: %s"
          % (len(manifest["frames"]), len(failed), out_dir))
    return 1 if failed else 0
