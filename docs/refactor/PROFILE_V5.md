# Confluence v5 follow-up: profile rules and approved projections

24 October 2026. This continues the foundation commits on the existing registered
`develop-refactor` worktrees. The full refactor is still incomplete and gated.

25 October identity follow-up: the current allowlist also permits approved FIN
(`approved_fin` -> `fayda_fin`) separately from approved UID/FAN16. FIN/FAN are
never converted. The revised lifecycle and current verification are described in
[REVIEW_A_FIXES.md](REVIEW_A_FIXES.md) and [ONBOARDING.md](ONBOARDING.md); dated
implementation checkpoints below remain historical.

The connected Atlassian reader verified page **203653123**, snapshot **v:5**,
updated **23 October 2026 at 09:43:47.742 UTC**. Its revision message requires a
local Farmer UPDATE CR and target approval for Kafka changes. The credential
file supplied by the user was not read because connected access succeeded.
The live metadata snapshot is in [references/](references/); the full text snapshot is retained in the workspace inventory directory.

The page proposes a broader Farmer profile and describes legacy geographic
attributes. The explicit implementation plan governs this refactor: only
identity/contact/location is copied, and geographic reviewer scope is resolved
through standard IAM/data policies. Household/economic fields, documents and
internal source workflow attributes remain excluded. No Confluence page or Jira
issue was changed.

## Implemented in both domain extensions

- Added one explicit canonical source-to-local profile allowlist. It includes
  Farmer ID, approved UID, names and local-language names, gender, birth date,
  phone/email, address and canonical administrative IDs/display names. This is
  an internal adapter contract, not a claim that raw DCI already returns every
  field in this shape. The authenticated source adapter is still required.
- Added the missing profile columns to the existing Farmer mixins, preserving
  model/table identities and existing fields. Live/history/intake schemas retain
  all profile fields, including address lines omitted by the pinned base schema.
  Crop's birth-date schema now matches its Date column. Additive SQL review
  artifacts are prepared in each worktree; they have not been executed or wired
  into deployment, and restored-copy rehearsal is still required.
- Wired strict identity normalization into the native Farmer domain validation
  methods. ASCII Farmer IDs and 16-digit UID strings preserve leading zeros;
  display whitespace is normalized. Partial drafts stay partial and do not
  onboard. Format validation does not establish an approved source match.
- Added pure update preparation: immutable source identity and fetched version
  must match ordering rules; omissions preserve approved optional values;
  explicit permitted clears survive. Source workflow/grants and business fields
  cannot enter the proposed local profile. A locked persistent version ledger
  and trusted signature/consent/source-approval verification remain outstanding.
- Added pure reviewer eligibility intersection. Both functional Farmer-approver
  eligibility and the resolved canonical Woreda policy scope are required. ADD
  selects verified source geography; UPDATE selects currently approved local
  geography. No administrator, unscoped, name-based or proposed-address fallback
  is provided. The production IAM/policy adapter and AWE expression binding are
  not yet implemented.
- Replaced pair-based Crop projection and Livestock sibling-submission owner
  inference with stable `farmer_uuid` references. Native Farmer post-approval
  hooks refresh linked active domain identity mirrors; native domain post-ingest
  hooks require an approved local dependency and use its current approved values.
  Missing/inactive/unapproved dependencies and conflicting identities raise
  visible errors within the caller transaction. No owner is selected for a
  legacy unlinked record; reviewed migration remains necessary.
- Identity projection refreshes only Farmer ID, UID, display name and identity
  search/display caches. The full profile stays in the local Farmer register.
  It preserves domain crop season/year, plots/animals/events, domain geography,
  histories and existing approval task IDs. It creates no intake and commits no
  independent transaction. Full-profile domain views remain unverified.
- Removed Livestock's obsolete fail-open enrichment method. No lookup failure
  path now claims that typed demographics establish trusted identity.

The same profile, geography and projection rule modules are byte-identical in
the two extension packages. The verification harness rejects drift. This is a
controlled source copy until a separately versioned common distribution is
approved; no new shared repository or platform dependency was invented.

## Verification and limits

Each registry has **21 profile/geographic rule tests** and **8 projection
transaction tests**. The latter use a real isolated SQLite database, exercising
rollback, absent approvals, missing links, conflicting identities, UID rotation,
late intake, optional clears and unchanged business/task fields. They do not
establish PostgreSQL concurrency or deployed workflow behavior.

The Docker checks run one registry at a time with networking disabled for tests.
Additional API/worker probes check the concrete native models, all three schemas,
native validation and domain naming/projection methods. The four changed package
roles are built under new `refactor-profile-v5` tags, preserving the foundation
images and evidence. CI now runs the profile/transaction tests using a separate
test-only image; production runtimes do not acquire its SQLite test dependency.

Each worktree records final results and image content IDs in
`docs/refactor/PROFILE_V5_VERIFICATION.json`, with logs under
`docs/refactor/evidence/profile-v5/`. Full application acceptance remains
**NOT RUN**. No registry application stack or live database was started or
modified for this follow-up. Existing Commons/Farmer runtime and user work were
preserved. At most one target registry's checks ran at a time.

## Still blocked

Confluence access is resolved. The remaining blockers in `STATUS.json` are unchanged
where they depend on unsupported platform hooks or unimplemented adapters:
durable linked ADD orchestration/reuse, signed consent-aware current-source lookup,
persisted source ordering, Kafka-created AWE tasks, production geographic policy
resolution/binding, full form/import/ODK/widget parity, scheduled jobs and
restored-database/live acceptance.

The Helm render and seed preflight gates remain active. Development/staging
deployment, legacy develop replacement and automatic assignment migration have
not been performed. The authoritative Farmer and upstream platform source remain
outside this change.
