#!/usr/bin/env python3
"""Copy HDRMerge and everything it needs into one self-contained folder.

    python packaging/bundle_hdrmerge.py [DEST]

The point of the bundle is that a person can drag one icon and be done, and
HDRMerge is the one thing this program cannot do without: it is the merge. A
build that finds it on the machine works on the machine that built it.

What comes along is measured, not guessed: `otool -L` walked transitively from
the binary, skipping `/usr/lib` and `/System` -- those are the OS and are always
there. On this Mac that is 25 files and 46 MB, most of it Qt 5, which HDRMerge
links even for its command line.

Plain libraries are flattened into one directory and every reference rewritten
to `@loader_path`, so the folder can sit anywhere. **The Qt frameworks keep
their layout**: flattening them into bare dylibs builds a bundle that segfaults
before it prints its own usage -- Qt reads its own framework directory at
startup, and a framework that is not one is not something it recovers from.

The last step is a signature. Editing a Mach-O header invalidates whatever it
had, and on Apple Silicon an unsigned binary does not run at all; an ad-hoc
signature is enough for that and is what PyInstaller does with its own.
"""

import os
import shutil
import subprocess
import sys

SYSTEM = ("/usr/lib", "/System")

# HDRMerge builds a QApplication even for a batch merge, and a QApplication on
# macOS refuses to start without the cocoa platform plugin -- "no Qt platform
# plugin could be initialized", and an abort, on a command line that was never
# going to open a window. So the plugin comes too, with a `qt.conf` beside the
# binary to say where it is. `offscreen` rides along because it is small and it
# is what a build machine with no display would want.
# `imageformats/libqjpeg` is not optional either: without it the merge still
# writes, and says "Error converting the preview to JPEG" and leaves the DNG
# without the embedded preview every other tool shows you in a file browser.
PLUGINS = ("platforms/libqcocoa.dylib", "platforms/libqoffscreen.dylib",
           "imageformats/libqjpeg.dylib")
QT_CONF = "[Paths]\nPlugins = plugins\n"


def which_hdrmerge():
    """Where HDRMerge is on this machine, following the symlink brew leaves."""
    found = shutil.which("hdrmerge") or "/opt/homebrew/bin/hdrmerge"
    if not os.path.exists(found):
        raise SystemExit("hdrmerge not found on this machine")
    return os.path.realpath(found)


def dependencies(path):
    """The non-system libraries a Mach-O file asks for, by absolute path."""
    out = subprocess.run(["otool", "-L", path], capture_output=True, text=True)
    libs = []
    for line in out.stdout.splitlines()[1:]:
        lib = line.strip().split(" (")[0]
        if lib.startswith(SYSTEM) or lib.startswith("@"):
            continue
        libs.append(lib)
    return libs


def inside_bundle(lib):
    """Where a library goes inside the folder, and what to call it from there.

    A framework keeps everything from its own `.framework` directory down, so
    `.../QtCore.framework/Versions/5/QtCore` becomes exactly that path under
    the destination. Anything else is a file in the top directory.
    """
    if ".framework/" in lib:
        head, tail = lib.split(".framework/", 1)
        return "%s.framework/%s" % (os.path.basename(head), tail)
    return os.path.basename(lib)


def closure_from(start, place, already):
    """The same walk, for a file that is not the binary and knows its place."""
    seen, queue = {}, [(start, place)]
    while queue:
        path, where = queue.pop()
        real = os.path.realpath(path)
        if real in seen or real in already or not os.path.exists(real):
            continue
        seen[real] = where
        queue.extend((dep, inside_bundle(dep)) for dep in dependencies(real))
    return seen


def closure(start):
    """Every library reachable from `start`, transitively.

    Keyed by the real path, valued by where it goes in the bundle -- keeping
    the *asked-for* path rather than the resolved one, because that is the
    name every other binary refers to it by.
    """
    seen, queue = {}, [(start, "hdrmerge")]
    while queue:
        path, where = queue.pop()
        real = os.path.realpath(path)
        if real in seen or not os.path.exists(real):
            continue
        seen[real] = where
        queue.extend((dep, inside_bundle(dep)) for dep in dependencies(real))
    return seen


def qt_plugins():
    """The platform plugins to carry, by absolute path."""
    prefix = subprocess.run(["brew", "--prefix", "qt@5"], capture_output=True,
                            text=True).stdout.strip()
    roots = [os.path.join(prefix, "share", "qt", "plugins"),
             os.path.join(prefix, "plugins")]
    out = []
    for name in PLUGINS:
        for root in roots:
            path = os.path.join(root, name)
            if os.path.exists(path):
                out.append(path)
                break
    return out


def finish_frameworks(dest):
    """Give each copied framework the symlinks macOS expects, and seal it.

    HDRMerge asks for its Qt by the full path -- `QtCore.framework/Versions/5/
    QtCore` -- so at *runtime* the bare Versions directory this used to copy is
    enough. `codesign` is stricter: a directory named `.framework` is a bundle,
    and one without `Versions/Current` and the two links beside it is "modified
    or invalid version", which fails the signature of everything containing it.
    That surfaced the moment the folder moved inside the app, because until
    then PyInstaller was flattening these into plain dylibs.
    """
    for name in sorted(os.listdir(dest)):
        if not name.endswith(".framework"):
            continue
        fw = os.path.join(dest, name)
        versions = os.path.join(fw, "Versions")
        if not os.path.isdir(versions):
            continue
        which = next((v for v in sorted(os.listdir(versions))
                      if v != "Current"), None)
        if not which:
            continue
        for link, target in (("Versions/Current", which),
                             (name[:-len(".framework")],
                              "Versions/Current/" + name[:-len(".framework")]),
                             ("Resources", "Versions/Current/Resources")):
            here = os.path.join(fw, link)
            if os.path.lexists(here):
                continue
            if not os.path.exists(os.path.join(os.path.dirname(here), target)):
                continue
            os.symlink(target, here)
        subprocess.run(["codesign", "--force", "--sign", "-", fw],
                       check=True, capture_output=True)


def say(text):
    """Progress, on stderr and unbuffered.

    Every step in here is a few dozen subprocesses with their output captured,
    so a silent run and a hung run look the same. They are not: collecting is
    about two seconds on a warm machine, and rather longer the first time
    macOS scans 55 MB of freshly copied binaries. Saying so is the difference
    between waiting and pressing ^C.
    """
    print(text, file=sys.stderr, flush=True)


def bundle(dest):
    """Write the folder. Returns the path of the binary inside it."""
    say("  hdrmerge: reading what it links")
    binary = which_hdrmerge()
    parts = closure(binary)
    plugins = qt_plugins()
    # From scratch every time: the files come out of Homebrew read-only, and a
    # second build tripped over its own first one rather than overwriting it.
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    for path in plugins:
        parts.update(closure_from(path, "plugins/" + os.path.relpath(
            path, os.path.dirname(os.path.dirname(path))), parts))
    os.makedirs(dest, exist_ok=True)
    say("  hdrmerge: copying %d files" % len(parts))

    # Copied first, then rewritten: `install_name_tool` needs the file it is
    # editing to be the one that stays.
    copied, where = {}, {}
    for real, place in parts.items():
        out = os.path.join(dest, place)
        os.makedirs(os.path.dirname(out) or dest, exist_ok=True)
        shutil.copy2(real, out)
        os.chmod(out, 0o755)
        copied[real] = out
        where[real] = place
        if ".framework/" in place:
            # Qt looks for the framework's own Info.plist beside the binary,
            # and `codesign` looks for it under the *version* -- a plist at the
            # top of a versioned framework is "bundle format unrecognized". It
            # goes where the version is; `finish_frameworks` links to it from
            # the top, which is the layout Apple documents and Qt ships.
            src_res = os.path.join(real.split(".framework/")[0] + ".framework",
                                   "Resources", "Info.plist")
            if os.path.exists(src_res):
                version = os.path.dirname(place)     # QtCore.framework/Versions/5
                dst_res = os.path.join(dest, version, "Resources", "Info.plist")
                os.makedirs(os.path.dirname(dst_res), exist_ok=True)
                shutil.copy2(src_res, dst_res)
                os.chmod(dst_res, 0o644)

    with open(os.path.join(dest, "qt.conf"), "w", encoding="utf-8") as fh:
        fh.write(QT_CONF)

    say("  hdrmerge: rewriting the paths inside them, and signing")
    for real, out in copied.items():
        here = os.path.relpath(dest, os.path.dirname(out))
        me = where[real]
        if me != "hdrmerge":
            subprocess.run(["install_name_tool", "-id",
                            "@loader_path/" + os.path.basename(out), out],
                           check=True, capture_output=True)
        for dep in dependencies(real):
            place = where.get(os.path.realpath(dep))
            if not place:
                continue
            subprocess.run(["install_name_tool", "-change", dep,
                            os.path.join("@loader_path", here, place), out],
                           check=True, capture_output=True)
        subprocess.run(["codesign", "--force", "--sign", "-", out],
                       check=True, capture_output=True)

    # Last, and after every install name is rewritten: sealing a framework
    # covers the binary inside it, and editing that binary afterwards would
    # break the seal again.
    finish_frameworks(dest)
    say("  hdrmerge: done")
    return os.path.join(dest, "hdrmerge")


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else "packaging/build/hdrmerge"
    out = bundle(dest)
    size = files = 0
    for root, _, names in os.walk(dest):
        for name in names:
            size += os.path.getsize(os.path.join(root, name))
            files += 1
    print("%s  (%d files, %.0f MB)" % (out, files, size / 1e6))
    check = subprocess.run([out, "--help"], capture_output=True, text=True)
    ok = "Usage: HDRMerge" in (check.stdout + check.stderr)
    print("runs from the bundle:", ok)
    left = [line for line in subprocess.run(["otool", "-L", out],
                                            capture_output=True, text=True
                                            ).stdout.splitlines()[1:]
            if not line.strip().startswith(("@loader_path", "/usr/lib",
                                            "/System"))]
    print("references outside the bundle:", left or "none")
    return 0 if ok and not left else 1


if __name__ == "__main__":
    sys.exit(main())
