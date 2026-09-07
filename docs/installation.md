# Installation

## The desktop builds

A macOS app and a Windows installer are on the
[releases page](https://github.com/jahurtado/hdrmerge-timelapser/releases). Both
carry their own Python **and their own HDRMerge**: nothing else has to be
installed, and nothing on this page applies to them.

Neither is signed with a paid developer certificate, so the first launch is
blocked once. On macOS, allow it through System Settings → Privacy & Security;
on Windows, click past the SmartScreen warning.

## From source

You need [uv](https://docs.astral.sh/uv/) and Python 3.9 or newer.

```sh
git clone https://github.com/jahurtado/hdrmerge-timelapser.git
cd hdrmerge-timelapser
uv sync --extra gui        # omit --extra gui on a machine with no display
uv run hdrmerge-timelapser
```

With no arguments it opens the window. `uv sync` installs into the project's
own environment rather than onto your PATH, which is why the command is run
through `uv run`.

## HDRMerge

A clone does not carry HDRMerge — only the bundles do — so it has to be on the
machine. It is what performs the merge; without it nothing can be produced.

Get it from [its own repository](https://github.com/jcelaya/hdrmerge), or from
your package manager where it is available (`brew install hdrmerge` on macOS).

It is looked for in this order, and the first one that exists wins:

1. the path given to `--hdrmerge`
2. the copy inside the bundle, if this is a bundle
3. `$HDRMERGE_BIN`, which may be set in a `.env` file beside the project
4. `hdrmerge` on the `PATH`
5. the default install location of the Windows build

If none of them has it, the program says so and stops rather than producing
half a sequence.
