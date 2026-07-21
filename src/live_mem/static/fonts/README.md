# Fonts — vendored WOFF2 binaries

These files are prebuilt, unmodified **latin subset** WOFF2 binaries as
distributed by Google Fonts, committed once at vendoring time — no runtime
CDN fetch (`docs/SECURITY.md` §3.5; CSP `default-src 'self'`), no
self-subsetting, no renaming. Same rationale as `static/vendor/README.md`
(reproducible, audit-able, CSP-strict self-hosting), applied to fonts
instead of JS libraries. Full license/upstream-repository detail lives in
`THIRD_PARTY_NOTICES.md`; this file pins the exact binaries.

## Pinned files

| File | Family | Weight | SHA-384 (base64) |
| --- | --- | --- | --- |
| `space-grotesk-600.woff2` | Space Grotesk | 600 | `UglM4y3uagIx+6rBW+5RRW5QEstMHpJATYPvjywetDYAySJDZkmPFZMMbhamYkPe` |
| `space-grotesk-700.woff2` | Space Grotesk | 700 | `dzNBpK7RDu9924/vftG9SLd0DYqA0R0++LY8OV8n2QiTkmPXhBEeVd9d1ARQGnmX` |
| `hanken-grotesk-400.woff2` | Hanken Grotesk | 400 | `swtRzCzR/B/QU/+t0y5GXqx5yXWJNgbZCKpW+jISW+P9+/ALRKFqBjHZPkIUMxBS` |
| `hanken-grotesk-500.woff2` | Hanken Grotesk | 500 | `nGJ+ZKxfQkUlxO9ka8kOCXxov9ePdqOqU8k4RG3RdY5CG8evgwM77VGIDvTtAzrj` |
| `hanken-grotesk-600.woff2` | Hanken Grotesk | 600 | `GCtbIWtw6ac+II9tcjrqo2z5xOOZxAykVWievl3MWNjIJx5K+Q8xi8P6QzCiWRDs` |
| `jetbrains-mono-400.woff2` | JetBrains Mono | 400 | `/4dCc3INKaFZBsZMku6bd44i8Gr1uXW1GYvdcNjsbHXHh2nGnk9G4RAhHf8hx3GT` |
| `jetbrains-mono-600.woff2` | JetBrains Mono | 600 | `CJcnMceWe1sniaYjmkgYIY3CKU5deQQZc389ekYG19zjKrIwnEmdCyVrjyXxdad+` |

Retrieved: 2026-07-09, via the Google Fonts CSS2 API (one single-weight
request per family+weight — batching multiple weights in one request can
silently collapse to duplicate variable-font URLs, so each weight was
fetched separately to guarantee a distinct static instance per file).

## Procedure to update or add a weight

```bash
cd src/live_mem/static/fonts
FAMILY_QUERY='Space+Grotesk'  # replace with the reviewed family query
WEIGHT=600                   # replace with the reviewed weight
curl -fsSL -H "User-Agent: Mozilla/5.0" \
  "https://fonts.googleapis.com/css2?family=${FAMILY_QUERY}:wght@${WEIGHT}&display=swap"
# extract the woff2 URL from the returned @font-face block (latin subset,
# unicode-range starting U+0000-00FF) and download it:
FONT_URL='https://fonts.gstatic.com/path/from-the-reviewed-css-response.woff2'
OUTPUT_FILE='space-grotesk-600.woff2'
curl -fsSLo "$OUTPUT_FILE" "$FONT_URL"

# verify + record the new hash
openssl dgst -sha384 -binary "$OUTPUT_FILE" | openssl base64 -A
```

Update this table and `THIRD_PARTY_NOTICES.md`'s "Vendored fonts" section
together. Do not self-subset or modify the font binaries: always use the
prebuilt Google Fonts artifact as-is so the recorded hashes and OFL provenance
remain reviewable from the public repository alone.
