#!/bin/sh
# Build the macOS application and wrap it in a disk image.
#
#     sh packaging/make-dmg.sh
#
# Everything the program needs is inside, HDRMerge included: the merge is what
# this tool is for, and asking somebody to install it first is asking them to
# do the packaging themselves.
#
# **Nothing here is signed with a Developer ID.** PyInstaller signs the binaries
# ad-hoc, which is what lets them run on Apple Silicon at all, but Gatekeeper
# wants more than that from something downloaded. Notarising needs Apple's
# Developer Program at 99 USD a year; without it the first launch is blocked
# once and the person has to allow it on purpose.
#
# arm64 only. PySide6 stopped shipping universal2 wheels, and the HDRMerge that
# goes inside is whatever this machine has -- so the file name says which.
set -e
here=$(cd "$(dirname "$0")" && pwd)
cd "$here"

# One build at a time. PyInstaller's `--clean` wipes a cache shared by every
# build on this machine, so a second run started while the first is working
# pulls the binaries out from under it -- and what that looks like is
# "Resource '…libOpenEXRCore….dylib' is not a valid file", forty seconds in
# and nowhere near the truth. `mkdir` is the lock because it is atomic.
mkdir -p build
if ! mkdir build/.building 2>/dev/null; then
    echo "A build is already running here. If it is not, remove" >&2
    echo "  $here/build/.building" >&2
    exit 1
fi
trap 'rmdir "$here/build/.building" 2>/dev/null' EXIT INT TERM

version=$(cd .. && .venv/bin/python -c \
    "import sys; sys.path.insert(0,'src'); import hdrmerge_timelapser as h; print(h.__version__)")
arch=$(uname -m)
dmg="dist/hdrmerge-timelapser-$version-$arch.dmg"

# HDRMerge is collected here and copied in after PyInstaller has finished,
# never handed to it: it is Qt 5 and PySide6 is Qt 6, and PyInstaller flattens
# whatever it is given into one Frameworks directory, where Qt 5 shadowed Qt 6
# and the window would not open. The folder is already relocatable -- every
# reference inside is @loader_path -- so a plain copy is the whole job.
echo "Collecting HDRMerge and what it links…"
../.venv/bin/python bundle_hdrmerge.py build/hdrmerge

echo "Building the app — a minute, and PyInstaller is loud…"
../.venv/bin/pyinstaller --noconfirm --clean hdrmerge-timelapser.spec

echo "Putting HDRMerge inside it, and sealing…"
app=dist/hdrmerge-timelapser.app
rm -rf "$app/Contents/Frameworks/hdrmerge"
cp -R build/hdrmerge "$app/Contents/Frameworks/hdrmerge"

# The same binary under a second name, outside Contents/MacOS, and it is what
# the window starts the work with. macOS decides that a process is *the app* --
# a Dock icon, a menu bar -- from the executable being the bundle's own, so
# running the merge as a subprocess put a second identical icon in the Dock for
# as long as the job lasted. Started through this name it checks in as
# BackgroundOnly instead, measured from the first tenth of a second, and the
# work is identical: same binary, same bytes.
#
# A symlink, not a copy: the bootloader resolves it to the real path and finds
# Frameworks/ from there, so nothing else is needed and the bundle does not
# carry 19 MB twice.
ln -sfn ../MacOS/hdrmerge-timelapser "$app/Contents/Resources/worker"

# Build leftovers that the bundle has no use for. `direct_url.json` is the
# worst of them: an editable install records where it was installed from, so it
# carries the absolute path of this checkout -- somebody's home directory,
# shipped in every DMG. The uv files are build timestamps. METADATA stays,
# because `cli.identity()` reads the author and the homepage out of it at
# runtime, and so does the About box.
for junk in direct_url.json uv_cache.json uv_build.json REQUESTED INSTALLER; do
  find "$app" -name "$junk" -path "*hdrmerge_timelapser-*.dist-info/*" -delete
done

# The licences, inside the bundle and before the signature: HDRMerge is GPL-3
# and travels whole in here, so the terms have to travel with it rather than
# living only in a repository the person who downloaded a DMG never saw. They
# go in before `codesign` on purpose -- anything added after it breaks the seal
# that the next line verifies.
mkdir -p "$app/Contents/Resources/licenses"
cp ../LICENSE                  "$app/Contents/Resources/licenses/LICENSE-GPL-3.0.txt"
cp ../THIRD-PARTY-LICENSES.md  "$app/Contents/Resources/licenses/"

# The seal has to cover what was just added. Verified rather than assumed:
# a bundle that signs but does not verify is one that Gatekeeper stops later,
# on somebody else's machine.
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"

rm -rf dist/dmg "$dmg"
mkdir -p dist/dmg
cp -R dist/hdrmerge-timelapser.app dist/dmg/
ln -s /Applications dist/dmg/Applications
# And once more in the disk image itself, where it is visible without anyone
# having to know that a .app is a folder you can look inside.
cp ../THIRD-PARTY-LICENSES.md dist/dmg/
cp ../LICENSE "dist/dmg/LICENSE-GPL-3.0.txt"
cat > "dist/dmg/First launch.txt" <<'TXT'
hdrmerge-timelapser
===================

Drag the app onto the Applications folder beside it.

The first time you open it, macOS will say it cannot be opened because
Apple cannot check it for malicious software. That is expected: this app
is not signed with an Apple Developer ID, which costs 99 USD a year.

To open it anyway, either

  * open  System Settings -> Privacy & Security,  scroll to the bottom,
    and press "Open Anyway" next to the message about it. Then open the
    app again and confirm. Once is enough, for ever.

or, if you prefer the one line,

    xattr -dr com.apple.quarantine /Applications/hdrmerge-timelapser.app

Nothing else needs installing. HDRMerge travels inside the app: it is
GPL-3 software by Javier Celaya, and THIRD-PARTY-LICENSES.md beside this
file says where its source is, along with everything else in here.

Apple Silicon only. On an Intel Mac, install from source instead:
https://www.elcacharrista.com
TXT
echo "Compressing the disk image — this one is slow…"
hdiutil create -volname "hdrmerge-timelapser $version" -srcfolder dist/dmg \
    -ov -format UDZO "$dmg" >/dev/null
rm -rf dist/dmg
# The checksum, beside the file and in the format `shasum -c` reads back, so
# whoever downloads it can check it with one command and no arguments. Written
# by the build rather than typed at release time: a hash worked out later is a
# hash of whatever happened to be in dist/.
( cd "$(dirname "$dmg")" && shasum -a 256 "$(basename "$dmg")" \
    > "$(basename "$dmg").sha256" )

echo "$dmg  ($(du -h "$dmg" | cut -f1))"
cat "$dmg.sha256"
