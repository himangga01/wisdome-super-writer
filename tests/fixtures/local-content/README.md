# Local housing workflow fixtures

Fixture mode assembles the sanitized source contracts from the sibling
`../applyhome/` and `../lh/` directories. It never performs network requests and
is always reported as fixture or dry-run output, never live acceptance. The
committed `manifest.json` pins all seven source files by SHA-256 before use.
