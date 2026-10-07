# Graph browser dependencies

Graph serves these reviewed browser artifacts locally. This removes executable
runtime dependencies on third-party CDNs and pins the exact bytes distributed
by the standalone service.

| File | Version | Source | SHA-384 (base64) |
| --- | --- | --- | --- |
| `marked.min.js` | 15.0.12 | <https://registry.npmjs.org/marked/-/marked-15.0.12.tgz> | `948ahk4ZmxYVYOc+rxN1H2gM1EJ2Duhp7uHtZ4WSLkV4Vtx5MUqnV+l7u9B+jFv+` |
| `marked.LICENSE` | 15.0.12 | same npm tarball | `61EZB/aHzoKVT71zYfgKtxFiu+RWPSi9UH34kVJPMyzaTcFdbvH0liFiUwpiBNqY` |
| `purify.min.js` | 3.4.15 | <https://registry.npmjs.org/dompurify/-/dompurify-3.4.15.tgz> | `uUMu9JDY09vBzRf9SPcK2VgUj+W/70J6Soc+Dded5P474ElQ63iv9j5N3DE7Kp3N` |
| `purify.LICENSE` | 3.4.15 | same npm tarball | `II9e1ieUDl5AxyiVq3/FflTua1Sr0kMJ25e6imG7rXg7SiAsA2VemsvEqVsLqM7/` |
| `vis-network.min.js` | 10.1.2 | <https://registry.npmjs.org/vis-network/-/vis-network-10.1.2.tgz> | `RDdG1CLOxjNlTHh4JYx/rnAueaMHbkBHmeHwrEyljMQw3LF0it4SkuNotIY/FPxD` |
| `vis-network.LICENSE` | 10.1.2 | same npm tarball (`LICENSE-MIT`) | `SgirdiWFoNFzo0m97+ao5+Gx0Sp3Chon9YHKkIDFLGtjPK/Ch2exIPbsuksnCsBg` |

The selected versions are those to which Graph's former unversioned CDN URLs
resolved on 2026-09-21. Both npm tarballs were verified against their registry
SHA-512 integrity values before the listed files were copied without
modification. The standalone vis-network bundle includes its upstream
`@egjs/hammerjs` and `core-js` runtime dependencies; their upstream notices are
retained in the generated bundle.
An OSV query on 2026-09-21 returned no records for npm `dompurify` 3.4.15,
`marked` 15.0.12, or `vis-network` 10.1.2. This is a dated inventory result,
not a security guarantee.

When updating a dependency, fetch its exact npm tarball, verify the registry
integrity, replace the artifact and licence together, update this inventory,
and run the Graph browser and static-asset tests.
