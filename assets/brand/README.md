# Hivemind brand assets

Logo assets for the Hivemind mark. These are the geometry files referenced by
[`README.md`](../../README.md) / [`README.fr.md`](../../README.fr.md) and by
the product UI (`src/live_mem/static/`); do not recolour the centre node.

| File | Use | Surface |
| --- | --- | --- |
| `hivemind-mark.svg` | Primary mark | Light / paper |
| `hivemind-mark-dark.svg` | Primary mark | Dark / `#0E141F` |
| `hivemind-favicon.svg` | Favicon, ≤24px (5-node reduction) | Light |
| `hivemind-avatar.svg` | GitHub org / app icon (rounded ink tile) | Any |
| `hivemind-lockup.svg` | Mark + `hivemind` wordmark | Light |
| `hivemind-lockup-dark.svg` | Mark + `hivemind` wordmark | Dark |
| `hivemind-icon-square.png` | Square app icon (raster) | Any |
| `hivemind-social-preview.png` | Repository social preview card | Any |

## Notes

- The mark is drawn on a **96-unit grid**: columns at x=23/73, rows at
  y=24 (`short`) / 48 (`mid`) / 72 (`long`). Clear space = one node diameter.
- The **centre consensus node is always Memory Amber** (`#F59E0B`). Never
  recolour it.
- Lockups render the `hivemind` wordmark in **Newsreader** (weight 500). The SVG
  declares a serif fallback, so exact letterforms require the Newsreader webfont
  to be installed/available. For pixel-exact lockups, outline the wordmark to
  paths in a vector tool before shipping to non-web surfaces.
- Need a different raster size? Rasterise the SVGs at export time — keep the
  SVGs as the single source.

## License and provenance

Copyright (c) the Hivemind maintainers. These assets are original artwork
distributed under the same [Apache License 2.0](../../LICENSE) as the rest of
this repository.

SHA-256 of each file at the time this table was last updated:

| File | SHA-256 |
| --- | --- |
| `hivemind-mark.svg` | `5d9bfc19715689c64efe86c0fb7ebb6452f095ab5a4d0e6613995125567c3a99` |
| `hivemind-mark-dark.svg` | `bfbfa9b4cbf0c16198d64a7eef3df10c0db5857bfaf1ad49ce0636d55056833f` |
| `hivemind-favicon.svg` | `c4fd622d33601c4ebb9aab6b83bede8018239083f1feb0a233682a7c4c48a5c6` |
| `hivemind-avatar.svg` | `85cb7cb664c8db2b370b32f00310bd0f19f5ed8439b74e91fe80bfba3d792121` |
| `hivemind-lockup.svg` | `cb95cc77533ae9a0c6370ac5879c011127ba29d84400346e8509fea87b870f9f` |
| `hivemind-lockup-dark.svg` | `a17374c20c3f6afc653f2985c48d31846b3ca551a8713ffdbeef7d2c0e21f8f4` |
| `hivemind-icon-square.png` | `39907a30d15666ed6b0ec7bb8c6c318e9ff0c8f6159b62d36793d4298be9336d` |
| `hivemind-social-preview.png` | `2d7b7cf6476d269b16ffc8e046e5c55ef612cba3cb952b7761f13dbb46296fd2` |

Regenerate this table (`shasum -a 256 assets/brand/*.svg assets/brand/*.png`)
whenever a file in this directory changes.
