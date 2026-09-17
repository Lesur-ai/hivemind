# Vendor — self-hosted third-party libraries

These files are copied from their public distribution endpoints and served
directly by Hivemind in order to:

1. Remove the runtime CDN dependency and its script-substitution risk.
2. Permit a strict Content Security Policy without external script origins.
3. Pin exact, independently verifiable artifacts for reproducible review.

## Pinned versions

| File            | Version | Source                                                                  | SHA-384 (base64)                                                       |
| --------------- | ------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `marked.min.js` | 12.0.2  | <https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js>              | `/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi`     |
| `purify.min.js` | 3.4.15  | <https://cdn.jsdelivr.net/npm/dompurify@3.4.15/dist/purify.min.js>      | `uUMu9JDY09vBzRf9SPcK2VgUj+W/70J6Soc+Dded5P474ElQ63iv9j5N3DE7Kp3N`     |

DOMPurify is the unmodified browser distribution from the
[official 3.4.15 release](https://github.com/cure53/DOMPurify/releases/tag/3.4.15).
Its bytes were verified against that GitHub tag, the pinned CDN artifact and
the npm tarball (with npm's SHA-512 integrity) on 2026-09-17. The upstream
copyright/license header is preserved; the upstream Apache-2.0 license text
is included in [`purify.LICENSE`](purify.LICENSE).

## Update procedure

```bash
cd src/live_mem/static/vendor
MARKED_VERSION=12.0.2   # replace with the reviewed target version
PURIFY_VERSION=3.4.15   # replace with the reviewed target version
curl -fsSLo marked.min.js \
  "https://cdn.jsdelivr.net/npm/marked@${MARKED_VERSION}/marked.min.js"
curl -fsSLo purify.min.js \
  "https://cdn.jsdelivr.net/npm/dompurify@${PURIFY_VERSION}/dist/purify.min.js"
curl -fsSLo purify.LICENSE \
  "https://raw.githubusercontent.com/cure53/DOMPurify/${PURIFY_VERSION}/LICENSE"

# Verify the hashes
for f in marked.min.js purify.min.js; do
    echo "$f sha384: $(openssl dgst -sha384 -binary "$f" | openssl base64 -A)"
done
```

Update this README and `THIRD_PARTY_NOTICES.md` with the new versions and
hashes in the same reviewed change.

## Why `marked` + `DOMPurify`?

- `marked`: converts Markdown to HTML for short notes and mid-memory files.
- `DOMPurify`: sanitizes the HTML produced by `marked`. Current `marked`
  versions do not provide a built-in `sanitize` option, so sanitization is an
  explicit client-side step after rendering.
