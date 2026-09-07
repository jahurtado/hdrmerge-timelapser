# Third-party software in the desktop builds

Running from source, this program is the code in `src/` and nothing else: pip
or uv fetch the libraries and their licences come with them.

The **desktop builds are different**. The macOS `.app` and the Windows folder
carry their own Python, their own Qt and their own copy of HDRMerge, so what
you download is one file containing work by a lot of other people under their
own terms. This is the list, and it is shipped inside the builds as well —
`Contents/Resources/licenses/` on macOS, `licenses\` on Windows — because a
notice that only exists in a source repository is not next to the thing it
describes.

## hdrmerge-timelapser itself

GPL-3.0-or-later. The full text is in [LICENSE](LICENSE), and the source is at
<https://github.com/jahurtado/hdrmerge-timelapser>.

## HDRMerge — the one that matters

**HDRMerge 0.5.0**, by Javier Celaya, GPL-3.0-or-later. It is bundled whole in
both desktop builds: on macOS as the binary plus its Qt 5 frameworks, on
Windows as the contents of its own install directory.

It is included because it *is* the merge. This program plans the merge and
develops the result; HDRMerge does the merging. A build that expected you to
install it first would be a build that does nothing on its own.

**Corresponding source.** The bundled HDRMerge is an unmodified upstream build
of version 0.5.0. Its complete corresponding source is at

> <https://github.com/jcelaya/hdrmerge> — release 0.5.0

which is where these directions point as GPL-3 section 6(d) allows: *"the
Corresponding Source may be on a different server (operated by you or a third
party) ... provided you maintain clear directions next to the object code
saying where to find the Corresponding Source."* If that repository ever stops
being reachable, ask through the address below and a copy will be provided.

Nothing in HDRMerge has been patched. The bundling script
(`packaging/bundle_hdrmerge.py`) copies the binary and the libraries it names,
rewrites their load paths so the folder can sit anywhere, and signs the result
ad-hoc; it changes no code.

## Qt

**LGPL-3.0.** Two versions travel, for two reasons: Qt 6 through PySide6,
which is this program's own window, and Qt 5 alongside HDRMerge, which links it
even for a command-line merge. Both are unmodified upstream binaries, used as
shared libraries and dynamically linked, which is what the LGPL asks for.

- Qt: <https://www.qt.io/> — source at <https://download.qt.io/archive/qt/>
- PySide6 / shiboken6: LGPL-3.0-only, <https://wiki.qt.io/Qt_for_Python>

## LibRaw

**LGPL-2.1 or CDDL-1.0**, reached through `rawpy` (MIT). LibRaw is what reads
NEF, CR2 and CR3, and what develops a DNG to check it. The wheel carries an
unmodified build.

- LibRaw: <https://www.libraw.org/> — source at <https://github.com/LibRaw/LibRaw>
- rawpy: <https://github.com/letmaik/rawpy>

## Python

**PSF-2.0.** The builds carry their own interpreter — a framework build on
macOS, the embeddable distribution on Windows — so nothing has to be installed.
<https://docs.python.org/3/license.html>

## The rest

Permissive, and none of them modified:

| package | licence |
|---|---|
| numpy | BSD-3-Clause (with 0BSD, MIT, Zlib and CC0-1.0 parts) |
| scipy | BSD-3-Clause |
| tifffile | BSD-3-Clause |
| imagecodecs | BSD-3-Clause |
| imageio | BSD-2-Clause |
| colour-demosaicing | BSD-3-Clause |
| colour-science | BSD-3-Clause |
| Pillow | MIT-CMU |
| typing-extensions | PSF-2.0 |

PyInstaller (GPL-2.0 with its bootloader exception) and the Inno Setup compiler
build the packages and are not part of them.

## Asking

<https://elcacharrista.com>
