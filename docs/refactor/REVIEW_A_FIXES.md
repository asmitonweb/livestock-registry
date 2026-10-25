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

## A-02: FIN/FAN alternatives and durable reference resolution

A new intake supplies FIN12 (`fayda_fin`), FAN16 (`fayda_fan_id`), or both.
A supplied Farmer ID must match the same approved source person; when omitted,
only the signed source response supplies it. Draft saving never starts onboarding.
The main FIN remains editable until verified SUCCESS; eSignet collects the FIN on
the verified path. No verification requirement was added to native finalization.

The signed, consent-aware lookup queries the declared identifier kind. Its server
record contract has separate `fin`, `fin_query_field`, `uid`, and `uid_query_field`
paths; default query fields stay `foundational_id` for compatibility. Source
approval, signature, correlation, freshness, immutable person ID and the profile
allowlist remain required. The profile adds only approved FIN. Notifications may
carry FIN or the existing FAN contract; they still trigger current-source lookup,
UPDATE CR and independent target approval, never demographic application.

No-match/unavailable lookup saves `coss_farmer_pending_lookups` with the original
identity, attempt count, next retry and diagnostic. A visible, server-owned intake
status explains that linking is pending. Native business finalization/approval
continues, but ingestion is held without an invented Farmer or consumed ingest
attempt. The registered extension workflow service retries approved pending
intakes, then creates/reuses the real Farmer ADD CR. Farmer approval releases the
original domain queue entry without recreating the business intake or decisions.
An identity mismatch is refused; unresolved or rejected dependencies never activate
new live domain records.

Advisory transaction locks and source constraints retain one reference/ADD for
concurrent submissions. Signed FIN/FAN aliases of the same immutable source person
reuse that reference without overwriting its approved profile or pending payload.
Each submission retains its signed identity/provenance binding. Later domain
stages may reuse only the server-read approved domain identity and reference;
the binding records that distinct provenance basis explicitly.

Existing databases need the additive migration
`ci/migrations/20261025_fin_fan_reference.sql` before new code starts. It adds
nullable Farmer FIN, domain reference-status and dependency-binding columns plus
the pending table. It does not rewrite records, documents, decisions, grants or
checkpoints. Rehearse on restored environment copies before promotion. Fresh
native DDL includes the same model fields. Metadata applies the FIN/FAN alternative
requirements and preserves section UUIDs and domain fields.

Verification is recorded in ONBOARDING_VERIFICATION.json and the seed-rehearsal
proof. These are isolated checks; live signed Farmer/consent, Kafka/AWE portal,
existing-environment preservation and user sign-in acceptance remain unrun. No
receiving registry runtime was changed by this batch.

2026-10-25 Livestock checks: fast native suite **123 passed / 82 dependency-gated
skips**; full isolated runner **20 checks PASS**, including 15 PostgreSQL
concurrency cases, 4 pending-lookup cases, 12 native CR cases, 3 native AWE HTTP
cases, 9 notification lifecycle cases and 9 signed HTTP lookup cases. Full native
metadata replay twice: identical, zero SQL errors. Prior-native synthetic database
restore: 100 tables preserved through two additive migration passes. New files
pass Ruff check/format; the pre-existing reference-module style findings remain
outside this batch. Actual Kafka/AWE engine and restored real-environment
acceptance are still NOT RUN.
