# -*- mode: python ; coding: utf-8 -*-
"""How the macOS bundle is built.

Two things make this bigger than the usual PyInstaller job.

**HDRMerge does not come through here.** It is a Qt *5* executable with its
own libraries, and PySide6 is Qt *6*: handed to PyInstaller as data, its
frameworks were flattened into `Contents/Frameworks` alongside PySide6's, where
`QtCore.framework/Versions/5` shadowed Qt 6's and the window died on the first
import -- `Symbol not found: _OBJC_CLASS_$_QDarwinPermissionHandler`. The
command line still merged, because that is the copy HDRMerge itself wanted. So
`make-dmg.sh` collects it with `packaging/bundle_hdrmerge.py` and copies the
folder into the app afterwards, whole and untouched: it is already relocatable,
every reference inside it is `@loader_path`, and nothing here rewrites it.

**PySide6 is trimmed.** A stock install is 1.2 GB because it ships every Qt
module; this window imports three, so the rest are excluded by name. The
scientific wheels cannot be pruned the same way: imagecodecs and scipy load
extensions by path, and dropping one shows up as a crash on the frame that
needs it rather than at build time.
"""

import os
import subprocess
import sys

from PyInstaller.utils.hooks import (collect_dynamic_libs,
                                    collect_submodules, copy_metadata)

HERE = os.path.abspath(os.getcwd())
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# `collect_submodules` returns the package itself first, and excluding that
# takes Qt out altogether -- which builds, and is lighter, and dies the moment
# the window is asked for. Keep the package and the three modules used.
QT_KEEP = {"PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"}
QT_DROP = [m for m in collect_submodules("PySide6")
           if m not in QT_KEEP and not m.startswith("PySide6.support")]

a = Analysis(
    ["app.py"],
    pathex=[os.path.join(ROOT, "src")],
    binaries=collect_dynamic_libs("imagecodecs"),
    # The package's own metadata comes along: `cli.identity` reads the author
    # and the homepage from it rather than from a constant, and a frozen build
    # without it falls back to what is written in the source -- which is the
    # copy that goes out of date.
    datas=[
        (os.path.join(ROOT, "src", "hdrmerge_timelapser", "icons"),
         "hdrmerge_timelapser/icons"),
    ] + copy_metadata("hdrmerge-timelapser"),
    # imagecodecs reaches its extensions with a computed __import__, so the
    # graph sees none of them. The whole package goes in rather than a guessed
    # list, because the symptom of guessing wrong is a decode failing on one
    # frame long after the build.
    hiddenimports=collect_submodules("imagecodecs") + collect_submodules("scipy"),
    # setuptools is build tooling and the app never imports it; PyInstaller
    # pulls a stub of it in behind some dependency, and with it a vendored
    # `Lorem ipsum.txt` -- filler text, the only file in the bundle that is
    # documentation of nothing. Out it goes, and pkg_resources with it, which
    # is the other half of the same package.
    excludes=QT_DROP + ["matplotlib", "tkinter", "PyQt5", "PyQt6", "IPython",
                        "pytest", "pandas", "notebook", "sphinx",
                        "setuptools", "pkg_resources"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="hdrmerge-timelapser", console=False,
          disable_windowed_traceback=False, argv_emulation=True,
          target_arch=None, codesign_identity=None, entitlements_file=None)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="hdrmerge-timelapser")

app = BUNDLE(coll, name="hdrmerge-timelapser.app",
             icon=os.path.join(ROOT, "src", "hdrmerge_timelapser", "icons",
                               "icon.icns"),
             bundle_identifier="com.elcacharrista.hdrmerge-timelapser",
             info_plist={
                 "CFBundleName": "hdrmerge-timelapser",
                 "CFBundleDisplayName": "hdrmerge-timelapser",
                 "NSHighResolutionCapable": True,
                 # Opening a timelapse means opening its folder, which is what
                 # lets a folder be dropped on the icon.
                 "CFBundleDocumentTypes": [{
                     "CFBundleTypeName": "Timelapse folder",
                     "CFBundleTypeRole": "Editor",
                     "LSItemContentTypes": ["public.folder"],
                     "CFBundleTypeOSTypes": ["fold"],
                 }],
             })
