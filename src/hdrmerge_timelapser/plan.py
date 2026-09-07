#!/usr/bin/env python3
"""The merge plan: the document the three commands pass between them.

`detect` measures a folder of raws and writes one of these; a person opens it in
the window and corrects what the measurements got wrong; `apply` reads it and
does the work. The separation is the whole design, and it is the same one
`eclipse-aligner` uses: measuring is slow and fallible, merging is slow and
irreversible, and a person belongs in between.

Two rules make the document usable rather than merely present.

**Every decision carries the evidence behind it.** Not `"blend_radius": 30` but
what each candidate radius would have spoiled, so the window can explain the
choice instead of asserting it, and a person disagreeing with it can see what
they are trading. The same for the steps of a bracket: each one records the
share of the frame it is the one to resolve, which is why two of them are left
out of a merge.

**A hand correction survives a re-detect.** Anything a person changed is listed
in `manual`, and detecting again recomputes everything except those. Without
that rule the first change of a global setting would throw away an afternoon of
corrections, and nobody would risk making the first one.
"""

import hashlib
import json
import os

VERSION = 1
NAME = "merge-plan.json"

# The fields of a frame a person may change, and therefore the only ones a
# re-detect must leave alone when they are listed in `manual`.
# `borrow` was here while the window had a switch for it. The window does
# not: taking a lent step out of a merge is unticking that step, so the
# decision is recorded in `steps` like every other one, and `borrow` is
# left as what detect measured -- who lent what, and from how far.
EDITABLE = ("include", "steps", "blend_radius", "anchor", "offset")


def path_for(source_dir, out_dir=None):
    """Where a folder's plan lives: beside the frames unless told otherwise."""
    return os.path.join(out_dir or source_dir, NAME)


def fingerprint(folder, raw_exts):
    """What the folder held when it was measured, cheaply enough to check often.

    Names and sizes, not contents: the question is only whether the folder is
    still the one the plan was written for, and re-reading four gigabytes to
    answer it would cost more than measuring again.
    """
    try:
        names = sorted(n for n in os.listdir(folder)
                       if os.path.splitext(n)[1].lower() in raw_exts)
    except OSError:
        return None
    sizes = []
    for n in names:
        try:
            sizes.append(os.path.getsize(os.path.join(folder, n)))
        except OSError:
            sizes.append(-1)
    return {"count": len(names),
            "digest": hashlib.sha1(
                ("|".join("%s:%d" % (n, s) for n, s in zip(names, sizes))
                 ).encode()).hexdigest()[:16]}


def stale(doc, folder, raw_exts):
    """Whether the folder has changed since the plan was written."""
    was = doc.get("source_fingerprint")
    if not was:
        return False              # written before this was recorded: say nothing
    now = fingerprint(folder, raw_exts)
    return bool(now and now != was)


def empty(source_dir, settings):
    """A plan with no frames yet, ready for detect to fill in."""
    return {
        "version": VERSION,
        "source_dir": os.path.abspath(source_dir),
        "detected": None,
        "source_fingerprint": None,
        "segmentation": None,
        "warnings": [],
        "settings": dict(settings),
        "frames": [],
    }


def load(path):
    """Read a plan, checking it is one and that this build understands it."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "frames" not in doc or "settings" not in doc:
        raise ValueError("%s is not a merge plan" % path)
    if doc.get("version", 0) > VERSION:
        raise ValueError("%s was written by a newer version (%s > %s)"
                         % (path, doc.get("version"), VERSION))
    return doc


def save(doc, path):
    """Write a plan, through a temporary so a failure leaves the old one.

    A half-written document that still parses is worse than no document: the
    window would open on it and the frames it lost would look like frames
    nobody wanted.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def mark(frame, field):
    """Record that a person set this field, so a re-detect will not undo it."""
    if field not in EDITABLE:
        raise KeyError("%s is not something a person can set" % field)
    if field not in frame["manual"]:
        frame["manual"].append(field)
    return frame


def carry_over(fresh, previous):
    """Put the hand corrections of `previous` back into a freshly detected plan.

    Frames are matched by their anchor -- the shot the merge is attributed to --
    because indices move as soon as anything is added or dropped, and a name
    does not. A correction whose frame no longer exists is reported rather than
    dropped in silence: it means the re-detect changed the shape of the plan,
    which is exactly when a person wants to know.

    Returns the list of corrections that could not be placed.
    """
    kept = {f["anchor"]: f for f in previous["frames"] if f.get("manual")}
    orphans = []
    for anchor, old in kept.items():
        new = next((f for f in fresh["frames"] if f["anchor"] == anchor), None)
        if new is None:
            orphans.append({"anchor": anchor, "manual": old["manual"]})
            continue
        for field in old["manual"]:
            if field not in EDITABLE:
                orphans.append({"anchor": anchor, "manual": [field]})
                continue        # a plan from a build that could edit this
            new[field] = old[field]
            mark(new, field)
    return orphans


def merged_steps(frame):
    """The frames HDRMerge will actually be given for this output frame."""
    return [s["frame"] for s in frame["steps"] if s["use"]]


def summary(doc):
    """One line per plan, for a terminal that has just written or read one."""
    frames = [f for f in doc["frames"] if f["include"]]
    merges = [f for f in frames if len(merged_steps(f)) > 1]
    manual = [f for f in doc["frames"] if f.get("manual")]
    # One vocabulary throughout: a row of the plan is a *bracketed set* -- of
    # several frames, or of one, whether because the rest were taken out or
    # because the camera shot no bracket -- and a *frame* is one exposure.
    # "merges" and "passthrough" were the words the code thinks in, and they
    # leaked onto the one line everybody reads.
    return ("%d sets (%d bracketed, %d single, %d skipped)%s"
            % (len(frames), len(merges), len(frames) - len(merges),
               len(doc["frames"]) - len(frames),
               ", %d edited by hand" % len(manual) if manual else ""))
