# Building the desktop bundles

The tool runs from source everywhere (see the README). This folder is only for
the two download-and-run bundles: the **macOS app** and its `.dmg`, and the
**Windows** portable zip and installer. Neither bundle is kept in the
repository; both are built here into `dist/`, which is gitignored.

Both carry their own Python, their own Qt **and their own HDRMerge**, so the
machine needs nothing — which is the whole point: HDRMerge is the merge, and a
build that asked the person to install the merge engine first would be a build
that does nothing on its own. Both are **unsigned** — a code-signing certificate
(Apple's, or a Windows one) is a yearly cost this project does not carry — so
each warns once on first launch and the person allows it on purpose. The two
platforms reach that result by opposite routes, and that is most of what this
folder is about.

## What is here

| File | Role |
|---|---|
| `make-dmg.sh` | macOS: build the `.app` and wrap it in a `.dmg` |
| `hdrmerge-timelapser.spec` | macOS: the PyInstaller recipe (`make-dmg.sh` drives it) |
| `app.py` | macOS: the bundle's entry point (strips the `-psn` launch arg) |
| `bundle_hdrmerge.py` | macOS: collect HDRMerge and its libraries into one relocatable folder |
| `make-win.ps1` | Windows: build the embeddable-Python payload and the `.zip` |
| `hdrmerge-timelapser.iss` | Windows: wrap that payload in an Inno Setup installer |
| `md_to_text.py` | both: render `THIRD-PARTY-LICENSES.md` to plain text for the installer |

The version is read from the package (`src/hdrmerge_timelapser/__init__.py`) in
both builds, so the file names and the metadata cannot drift from `--version`.

## macOS

```sh
uv venv
uv pip install -e '.[gui]'      # PySide6, the window
uv pip install pyinstaller      # or: uv sync --group packaging
sh packaging/make-dmg.sh
```

PyInstaller freezes a self-contained `.app`; `make-dmg.sh` wraps it in the
ordinary drag-to-Applications disk image, first-launch note included. **Apple
Silicon only** — PySide6 stopped shipping universal2 wheels, so an Intel Mac
cannot run this build, and the file name says `arm64`.

HDRMerge does **not** go through PyInstaller. It is a Qt *5* executable with its
own libraries, and PySide6 is Qt *6*; handed to PyInstaller as data, its
frameworks flatten into `Contents/Frameworks` where `QtCore.framework/Versions/5`
shadows Qt 6's and the window dies on the first import. So `make-dmg.sh` collects
it with `bundle_hdrmerge.py` — `otool -L` walked transitively, the Qt frameworks
kept in their layout, every plain library rewritten to `@loader_path` — and
copies the folder into the app afterwards, whole and untouched. The reasoning
lives in the spec's docstring.

## Windows

```powershell
uv venv
uv pip install -e ".[win]"      # PySide6 only
powershell -File packaging\make-win.ps1
```

That produces `dist\hdrmerge-timelapser\` (the payload) and
`dist\hdrmerge-timelapser-VERSION-x64.zip`. The zip is the whole deliverable:
unpack and run `hdrmerge-timelapser.cmd`. **x64 only.**

For the installer, install [Inno Setup](https://jrsoftware.org/isinfo.php) once
and compile the payload `make-win.ps1` left behind:

```powershell
winget install JRSoftware.InnoSetup
iscc packaging\hdrmerge-timelapser.iss
```

That writes `dist\hdrmerge-timelapser-VERSION-x64-setup.exe` — a per-user install
(no admin prompt) with a Start-menu shortcut and an uninstaller.

### HDRMerge has to be installed to build the Windows bundle

The bundle carries its own HDRMerge, and `make-win.ps1` gets it by copying an
install on the build machine — `C:\Program Files\HDRMerge`, or `$env:HDRMERGE_DIR`
if you point it elsewhere. This is the same promise the macOS build makes, and
the same requirement: the mac build needs a HDRMerge to copy too. Without one
`make-win.ps1` **stops on purpose**, rather than shipping a bundle missing the
one thing it cannot merge without.

Install the Windows HDRMerge from
[github.com/jcelaya/hdrmerge](https://github.com/jcelaya/hdrmerge) (its installer
lands in `C:\Program Files\HDRMerge`), or set `HDRMERGE_DIR` to the folder that
holds `hdrmerge.com`, then build. The Windows HDRMerge is already a
self-contained folder — `hdrmerge.com` (the console build, whose output the
window reads), `hdrmerge.exe`, and their libraries beside them — so unlike macOS
there is no dependency walk: the whole directory is copied as it stands.
`resolve_hdrmerge()` looks for it first, at `<payload>\hdrmerge\hdrmerge.com`, so
the bundle never depends on what happens to be on the machine that runs it.

## Checksums

Each script writes a `.sha256` beside the file it just made, in the format
`shasum -c` and `sha256sum -c` both read back — lower-case hex, two spaces, the
bare name. It is done by the build and not by hand at release time, because a
hash worked out afterwards is a hash of whatever happened to be left in
`dist/`.

`iscc` runs after `make-win.ps1`, so the installer is the one artefact neither
script has hashed. Same helper, one line:

```powershell
$f = "dist\<name>-<version>-x64-setup.exe"
[IO.File]::WriteAllText("$f.sha256",
    "$((Get-FileHash -Algorithm SHA256 $f).Hash.ToLower())  $(Split-Path -Leaf $f)`n")
```

At release time the three come from two machines, so gather them into one file
rather than uploading three:

```sh
cat *.sha256 > SHA256SUMS      # and verify with:  shasum -c SHA256SUMS
```

### Why Windows does not use PyInstaller

It was tried first and abandoned. PyInstaller's bootloader trips a Windows
Defender false positive (`Program:Win32/Vigram.A`); Defender then silently
**truncates the compiled `setup.exe`**, and it fails at install with "The setup
files are corrupted." Signing would not help — Defender is matching a byte
pattern, not the absence of a signature.

So Windows ships an **embeddable CPython** instead: `python.exe`, its stdlib
zip, and a folder of ordinary wheels. There is no frozen binary, so Defender has
nothing to object to. `make-win.ps1` downloads the embeddable Python (pinned to
the venv's exact version, because the wheels are for that ABI), installs the
project into it with `uv` (a uv-made venv has no `pip`, so it is
`uv pip install --target`), copies HDRMerge in, and lays a `.cmd` launcher and
the icon beside it. The window is launched with `pythonw.exe -m
hdrmerge_timelapser`; `detect` and `apply` run through
`python\python.exe -m hdrmerge_timelapser`.

### Things that bit, and are handled

- **PySide6-Essentials, not the full PySide6.** The full wheel drags in
  pyside6-addons — Qt WebEngine (a 198 MB DLL by itself), Quick, 3D, Charts —
  none of which a widgets window touches. Essentials supplies the same `PySide6`
  package, only the parts used, and saves ~370 MB.
- **The QML tree and the Qt dev-tool exes are pruned.** Even Essentials ships
  `PySide6\qml`, whose build objects sit at 260 characters — Windows' path limit
  — and the Inno compiler cannot read past it ("cannot find the path specified").
  The QML language server, Designer, Linguist and the rest go too: a widgets
  runtime never runs any of them, and one, `qmlls.exe`, is a file the compressor
  stumbled on before.
- **The `.pyi` stubs are kept.** scikit-image's lazy loader reads its own
  `__init__.pyi` at runtime; deleting it to save space turns every skimage import
  into a crash.
- **HDRMerge is copied whole.** It is already self-contained, so the whole
  install directory goes in as it stands rather than being walked — see above.
- **The uninstaller removes the whole folder.** Python writes `.pyc` caches
  beside the code on first run; Inno only tracks what it installed, so without an
  explicit `[UninstallDelete]` the folder lingers full of bytecode.

### The `.iss` uses `lzma`, not `lzma2`

On this payload Inno's `lzma2` codec writes a stream that its own decompressor
rejects at install — `lzmadecomp: Compressed data is corrupted` — on a large
required DLL: numpy's bundled OpenBLAS, ~20 MB, which cannot be pruned the way
the QML tools were. The source file is intact and the plain `lzma` (v1) codec
compresses it to the same size without the fault, so the whole build hangs on
this one word. `SolidCompression` stays off so a bad block can never poison more
than its own file. (This is a different failure from the sibling project
`eclipse-aligner`, whose lighter payload does not trip it and stays on `lzma2`.)

### If `iscc` produces a "source file is corrupted" installer

A freshly compiled `setup.exe` can fail at install with "the source file is
corrupted" for two unrelated reasons, and the compile reports success either
way — the bad block only surfaces on extraction.

1. **The `lzma2`/OpenBLAS fault above** — already handled by `Compression=lzma`
   in the `.iss`. If you changed it back to `lzma2`, change it back.
2. **Defender scanning `packaging\dist` the first time** those exact files
   appear, right after `make-win.ps1` writes them; an occasional locked read
   makes `iscc` copy a bad block. Recompile — the second pass almost always
   succeeds — or exclude the build output once (elevated PowerShell) and
   recompile:

   ```powershell
   Add-MpPreference -ExclusionPath "$PWD\packaging\dist"
   ```

   This is a build-machine step only; it stamps nothing into the installer. The
   zip is never affected — it is stored, not LZMA-compressed by Inno.

Either way, **verify a build by installing it once** (`setup.exe /VERYSILENT
/DIR=...` then check the files are there): a corrupt block is silent at compile
but obvious at install.
