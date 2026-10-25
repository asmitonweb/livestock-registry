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

## A-05 / A-07: field patterns, person names and supplied dates

Editable Farmer ID, FIN and FAN widgets use issuer patterns and translated
problem-and-fix messages. FAN accepts compact or space-separated digits, and FIN
and FAN stay separate. The FIN/FAN conditional requirement uses the pinned native
widget operator `empty`; A-02's unsupported `isEmpty` spelling is corrected.
Verify still needs no FIN on the main form.

`register_domain/services/field_rules.py` checks supplied fields through every
registered domain service's `validate_domain_attributes` entry point. It trims
person-name/identifier whitespace; names allow letters, spaces, hyphens and
apostrophes, have a 100-character bound and reject digits, underscores and emoji.
Ethiopic names work, and explicitly Amharic fields require Ethiopic letters.
Botanical, animal, coded-list and account-username fields keep their separate
contracts. Livestock veterinary and AI-technician names are included. Table errors
identify the row without echoing personal values. Only the native `edit_action:
DELETE` contract skips these checks; legacy client delete flags cannot bypass them.

All Date-typed domain fields reject supplied malformed dates and dates before
Gregorian 1900, while optional blanks stay optional. Existing accepted display
formats normalize to ISO GC. Registration's native `today` default remains valid.
Birth/registration dates also reject future values; existing domain-specific
chronology rules still run. Native date metadata supplies the matching minimum
and past-only setting for those historical fields. The EC widget port and old EC
string twins are separately tracked under A-06; this batch does not claim that
calendar/UI work is complete.

Managed Farmer CR snapshots trim the same name whitespace before the frozen
payload is stored, preserving signed-response provenance and every other profile
value. The normalized proposed value still needs native target approval.

Translations run after native language-row creation, so the first installation
and repeated seed agree. Existing custom message wording wins over defaults.
The metadata rehearsal now exits with failure on SQL errors or differing repeated
configuration, in addition to retaining the evidence JSON.

`ci/refactor/widget_rules_probe.mjs` executes the pinned native widget package
against effective metadata, with no compiled-asset edits. Run after the normal
UI materialization/dependency step:

```sh
node ci/refactor/widget_rules_probe.mjs docs/refactor/new-dev/target-pass-1.json ui/staff-ui/.app/node_modules/@openg2p/registry-widgets/dist/index.esm.js
```

`evidence/field-widgets.json` records package version, hashes and actual native
engine assertions. This establishes identity/name engine behavior and date-bound
configuration. Browser blur/Next/CR interactions and live receiving acceptance
remain NOT RUN. No receiving runtime or existing records changed in this batch.

2026-10-25 Livestock A-05/A-07 verification: **160 fast native tests passed**, 82
DB/HTTP-gated skips; the full isolated runner passes all **20 checks**, including
all native CR, workflow HTTP, notification and signed lookup cases. Full metadata
replay twice is identical with zero SQL errors. The pinned native widget engine
passes **93 assertions** over six editable identity widgets, nine person-name
widgets, 16 date-bound configurations and one conditional FAN widget. New Python
files pass Ruff check/format. These are source/isolated checks; live receiving
runtime/browser acceptance remains unrun.
