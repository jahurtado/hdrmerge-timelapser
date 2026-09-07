# Changelog

## 1.0.0

The first release.

Three steps and one document between them: the first pass measures a folder of
bracketed raws and writes `merge-plan.json`, the window shows what it decided
and lets you correct it, and producing the sequence is a separate, deliberate
step. Nothing writes an image until that last one.

- Segments a timelapse into brackets from the EXIF alone, and works out per
  bracket which exposures are worth merging and how far to feather HDRMerge's
  layer mask.
- Every decision in the document carries the measurement behind it — what each
  exposure resolves, what each candidate blend radius would spoil — so the
  window can explain rather than assert.
- A hand correction survives a re-measure: edited fields are recorded as
  manual, and measuring again recomputes the rest around them, matching frames
  by anchor rather than by index.
- Six output formats, each with what it costs a frame and how to interpret it:
  scene-linear EXR at 32 or 16-bit half float, HDRMerge's float DNG straight
  through, integer DNG at 14 or 16 bit, and integer TIFF either scene-linear or
  with an sRGB curve.
- Frames with no bracket are carried into the sequence too, so the output is
  the whole timelapse.
- Desktop builds for Apple Silicon and x64 Windows, each carrying its own
  Python and its own HDRMerge.
