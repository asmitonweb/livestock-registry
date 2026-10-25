# Review A fixes

## A-01: one stored FAN representation

Domain validation and the signed-reference profile use the same ASCII FAN16
normalizer. Both compact input and four spaced groups store 16 digits, preserving
leading zeroes. FIN12, Unicode digit lookalikes, numeric coercion and FAN prefixes
are refused with an explained field message. FIN is not converted into FAN.

2026-10-25: full native suite 106 passed, 65 database-dependent skips; eight direct
domain format regressions per registry. Full native SQL metadata replay twice
passed with zero errors and identical metadata. No registry runtime changed.
The acceptance report is docs/registries/review-20261025/A-crop-livestock.md in
the workspace; later findings remain open until separately delivered.
