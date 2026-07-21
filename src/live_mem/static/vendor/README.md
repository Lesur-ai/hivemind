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
| `purify.min.js` | 3.1.6   | <https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js>       | `+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a`     |

## Update procedure

```bash
cd src/live_mem/static/vendor
MARKED_VERSION=12.0.2   # replace with the reviewed target version
PURIFY_VERSION=3.1.6    # replace with the reviewed target version
curl -fsSLo marked.min.js \
  "https://cdn.jsdelivr.net/npm/marked@${MARKED_VERSION}/marked.min.js"
curl -fsSLo purify.min.js \
  "https://cdn.jsdelivr.net/npm/dompurify@${PURIFY_VERSION}/dist/purify.min.js"

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
