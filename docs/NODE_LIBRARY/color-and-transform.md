# Color and transform - working in the right space

Nodes and techniques for color-space conversion and manual (non-AI) pixel geometry: scale, rotate, distort,
warp, skew, crop, resample. The headline is a production-color practice from compositing, and we already ship a
node that implements it.

## TECHNIQUE: do manual transforms in LOG, not linear

**Rule:** any manual, non-AI change to pixels (scale, transform, rotate, distortion, warp, skew, any resample)
should be done in a LOG-encoded space, then converted back, rather than on linear data. Linear resampling
crushes detail in highlights and shadows; log encoding spreads code values perceptually so interpolation
preserves it.

**Flow:**
```
image ──▶ Linear→Log ──▶ [scale / transform / distort / warp / skew] ──▶ Log→Linear ──▶ continue
```

- **Source / status:** *confirmed.* Standard practice in The Foundry's Nuke (OCIO `OCIOLogConvert` /
  `OCIOColorSpace`) and the kit owner's own pipeline. Feed LINEAR data (linearize sRGB first); carry float
  through the wrap; convert back before any node that expects linear / sRGB.
- **Why it works:** a log curve allocates more code values to darks and compresses brights the way perception
  does, so resampling in that space keeps tonal detail linear interpolation would average away. Same reason
  film scans and ACEScct grade in log.
- **Anti-patterns:** do NOT run AI / diffusion / VAE steps in log (they expect linear or sRGB); do NOT
  round-trip at 8-bit (carry float); one wrap around the whole transform block, not per node.

### REDACTEDLogConvert  (display: "REDACTED Linear <-> Log (ACEScct)")  -- the node we ship for this
- **pack / source:** `ComfyUI-REDACTED` (the owner's own pack) | **category:** `REDACTED/color` | **I/O confirmed via get_node_info:** 2026-06-30
- **purpose:** the Linear<->Log transfer that makes the technique above one node. Despite the pack name it is a
  generic color node, not dome-specific.
- **inputs:**
  - `image` (IMAGE) - treated as LINEAR for `linear_to_log`; as ACEScct-log for `log_to_linear`.
  - `operation` (combo: `linear_to_log` / `log_to_linear`) - `linear_to_log` before the transforms, `log_to_linear` after.
- **outputs:** `image` (IMAGE) - converted, HDR range preserved (no [0,1] clip).
- **how it works:** an ACEScct log transfer. Reversible to ~1e-14, so the wrap is lossless apart from the
  transform itself.
- **strengths:** HDR-safe (no clipping), reversible, one node per direction, no OCIO config / dependency.
- **bugs / lags + fixes:** none known. It expects LINEAR in; feeding sRGB without linearizing first gives a
  wrong curve (linearize sRGB -> `linear_to_log` -> transform -> `log_to_linear`).
- **anti-patterns:** see the technique anti-patterns above. Not a tonemapper; it does not map HDR to SDR.
- **placement:** wrap it tightly around the manual geometry: `linear_to_log` -> transform / scale / distort ->
  `log_to_linear`. Author: `ComfyUI-REDACTED` (owner's pack).

### OCIOColorConvert / OCIOLogConvert (Nuke origin, not installed here)
The Nuke equivalent the practice comes from. *Inferred, not confirmed on this machine:* `get_node_info OCIO`
returned nothing (no OCIO pack installed, 2026-06-30). You do not need it: `REDACTEDLogConvert` already
provides the Linear<->Log transfer. If you ever want true OCIO config-driven conversion, `search_custom_nodes
"OCIO" / "OpenColorIO"`, install, then confirm its real I/O via get_node_info before composing.

## Native linear / HDR / EXR I/O (confirmed)

You do not need OCIO to persist linear or HDR. Confirmed via get_node_info 2026-06-30:
- **`SaveImageAdvanced`** (core, `image`): PNG 8/16-bit; EXR 32-bit float; `input_color_space` sRGB / HDR
  (HLG Rec.2020) / linear (scene-linear Rec.709). EXR always stored scene-linear.
- **`REDACTEDSave`** (`ComfyUI-REDACTED`): ProRes 4444 / 422HQ (.mov 10-bit, HDR headroom) or H.264 preview;
  sequence as `exr_f16` / `exr_f32` / `tiff_16` / `png_16` / `png_8` (EXR/TIFF keep real HDR range). See
  `custom-author.md`.
- **`LTXVHDRDecodePostprocess`** (`ComfyUI-LTXVideo`): decompresses LogC3 HDR IC-LoRA output + Reinhard
  tonemap; outputs `tonemapped` (SDR) + `hdr_linear`, optional EXR sequence (needs
  `OPENCV_IO_ENABLE_OPENEXR=1`). See `custom-author.md`.
- **anti-pattern:** plain `SaveImage` is 8-bit sRGB PNG only; it discards the precision the log workflow exists
  to protect.

## Status
Technique: confirmed (Nuke / OCIO standard + owner pipeline). `REDACTEDLogConvert`, `SaveImageAdvanced`,
`REDACTEDSave`, `LTXVHDRDecodePostprocess`: I/O confirmed via get_node_info 2026-06-30. OCIO node: inferred
(not installed), and not needed.
