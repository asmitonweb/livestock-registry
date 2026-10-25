# Farmer reference onboarding and approved updates

This is an extension implementation candidate, not a completed refactor or live
acceptance certificate. Installation/promotion gates remain enabled. The user
confirmed FIN12 **or** FAN16 as the receiving identity. Store FIN only in
`fayda_fin` and FAN only in `fayda_fan_id`; never pad or convert either.
A supplied Farmer ID must match; an omitted Farmer ID comes only from the
authenticated source response. See REVIEW_A_FIXES.md for the updated A-02 contract.

## Implemented ownership and lifecycle

`farmer_reference/lookup.py` sends a signed DCI expression request with the declared
FIN and/or FAN (and Farmer ID when supplied), a fresh signed consent assertion, and the active-record
filter. It verifies the response with a configured source public key and key
ID, correlates message/transaction/reference IDs, requires signature and consent
enforcement, checks freshness/cardinality/approval provenance, and applies only
the identity/contact/location allowlist. Source URLs/record mappings come from
deployment configuration. Redirects are not followed. HTTP failures, ambiguous
records, invalid signatures and incomplete source contracts cannot establish a
trusted reference. Unavailable/unresolved lookups retain a durable pending marker
and a visible intake diagnostic; finalization and business approval remain allowed.
A signed identity mismatch is refused. Live activation always requires the approved
Farmer dependency. A notification is never a demographic data source.

`ReferenceIntakeService` is a registered subclass of the native intake service.
Draft save does not call onboarding. Final submission captures the declared identifier(s) on the
domain root, looks up the approved source, binds the server-selected local
Farmer UUID and creates/reuses a native Farmer ADD CR in the same transaction.
An existing approved exact reference is reused without overwriting its approved
profile. Later business intakes on an existing approved domain record reuse its
stable approved reference. They do not onboard the Farmer again.

The five extension-owned `coss_farmer_*` tables retain source provenance,
source ordering, submission dependencies, unresolved lookups and AWE delivery. PostgreSQL
transaction locks cover the absent-reference case; uniqueness constraints cover
source identity, local UUID, UID and request versions. A pending ADD does not
insert a live Farmer. CR/payload/correlation/outbox changes roll back together.
The original intake creator remains the ADD requester, so the standard AWE
self-approval policy can apply. No partner is created; source attribution uses
the configured existing source partner or native system attribution.

Generic native root-register CR creation accepts UPDATE, not ADD. The extension
therefore owns its narrow source-validated reference ADD orchestration, calls
native CR construction, and uses a registered approval-service subclass to call
native primary-section insertion. It retains native history, documents,
outgestion, sequence checks and completion processing. The verified snapshot and
persisted source version are checked before application. Arbitrary Farmer edits,
manual reference approvals, modified managed payloads and direct Farmer intake
ingestion are blocked. AWE owns the decision through its native authenticated
webhook. Pending ADD task presentation and record-policy access still need real
portal acceptance; lower-level service tests do not establish that UI behavior.

Domain approval can arrive first. The original submission remains approved,
while ingestion is held with a dependency diagnostic and zero worker attempts.
Farmer approval releases that same native queue entry; a periodic recovery pass
releases rows skipped because another transaction held a lock. Business tasks
and approvals are not recreated. Rejected dependencies cannot activate a domain
record. A valid native import/ODK intake reaches this same finalization service
through the unmodified workers; full transformation and media parity are not yet
accepted.

The legacy staff `approve_submission` endpoint is blocked for these registries;
domain decisions use the native session-aware AWE callback. This prevents that
older endpoint from queuing ingestion without the dependency hold.

For notifications, `ReferenceIngestionService` requires the configured existing
source partner and an already approved local reference. It fetches current
source data by the stable source identity, creates/reuses a native UPDATE CR and
outbox entry, and leaves approved local values unchanged until local approval.
Duplicate versions reuse the existing request, including rejection. Versions
older than the applied version cannot overwrite it. UID rotation can only apply
to the same verified source person and still requires source consent enforcement
and target approval. Omitted optional fields are preserved. Name caches and
linked domain identity mirrors refresh after approval; business geography,
season/year, counts, events and prior business decisions remain domain-owned.

## Native worker and AWE extension loading

The published `run_celery.py` supports `CELERY_IMPORTS`. The thin COSS Celery
image sets `openg2p_registry_extensions.farmer_reference.worker`, consistently
using the native selected-extension alias. It registers the same service
subclasses used by the APIs and adds one extension task/schedule without
replacing native workers, the native Celery app, or its existing schedules.
Registration is idempotent. The delivery task uses a short-lived extension
database engine so its event loop does not reuse another native loop's pool.

The committed outbox calls native AWE integration with `cr-{id}` idempotency.
It obtains a service bearer, supplies only server-resolved reviewer context and
requires the separate registry-owned Farmer-reference policy key. Failed
deliveries retain safe diagnostic codes, emit an operator log and expose a
blocked native CR workflow summary. Five failed deliveries become BLOCKED;
native worker retry limits are not increased. An operational retry/recovery
procedure still requires live acceptance.

`PolicyDirectory` reads standard functional roles and published `DP_` assignments
through Keycloak Admin REST, plus policy metadata in the receiving registry. It
does not read Master Data's database, use custom location attributes, resolve
query-string secrets or change identity grants. Its supported scope subset is
finite Farmer `REGISTER_RECORD` Woreda-ID EQ/IN policies and AND/OR groups;
DISALLOW removes scope. Unknown/broad filters block routing. Other existing
policy forms, including GEO-target policies, require supported migration or
additional accepted resolution before promotion. ADD uses source geography;
UPDATE uses currently approved local geography. No administrator/unscoped
fallback exists.

`configuration/farmer-reference-awe-policy.json` defines exactly one Woreda
stage, `on_empty=block`, using AWE's JSONLogic expression
`{"var":"farmer_reference_approver_ids"}`. Fresh metadata binds this policy to
the Farmer identity section independently of domain-intake policy. These
artifacts are not installed into shared AWE by the tests. Existing bindings and
geographic assignments are never automatically deleted or rewritten.

## Deployment inputs and source gap

Livestock also loads `openg2p_registry_extensions.tasks` through the same native
comma-separated `CELERY_IMPORTS` setting. Its five retained domain sweeps use
standard shared tasks and additive schedules: outbreak alerts every 900 seconds;
audit retention, vaccination/status, pending-approval and calving reminders every
86400 seconds. Task names and service logic are preserved. They obtain the native
worker engine when executed, with no import-time connection or beat-app
replacement. The loader probes verify every task and schedule and compare all
pre-existing native entries. The sweeps have not run against environment data;
retention/notification behavior still needs its acceptance evidence.

All names below have the `COSS_FARMER_` environment prefix:

| Input | Meaning |
| --- | --- |
| DCI_URL, SENDER_ID, SOURCE_ID | Existing signed lookup endpoint and partner/source identities |
| SIGNING_KEY_FILE, SIGNING_KID, SIGNING_ALGORITHM | Existing caller signing key mount; RS256 or EdDSA |
| SOURCE_PUBLIC_KEY_FILE, SOURCE_KID, SOURCE_ALGORITHM | Pinned source verifier; never an untrusted response key URL |
| CONSENT_AUDIENCE, CONSENT_CONTROLLER, CONSENT_PURPOSE, CONSENT_SCOPES | Existing consent policy inputs |
| RECORD_CONTRACT_FILE | Server-owned JSON mapping matching `RecordContract`; not a client trust flag |
| SOURCE_PARTNER_ID | Exact receiving connector classification partner ID; reused, not provisioned here |
| AWE_TOKEN_URL, AWE_CLIENT_ID, AWE_CLIENT_SECRET_FILE | Service account for native workflow submission |

Private values belong in existing secret mounts/deployment configuration, never
Git or logs. Do not enable disabled-signature or disabled-consent fallbacks.

The inspected OAN Farmer source at `4901f11dba9e4debf710e182b769120f5906892a`
describes `foundational_id` as FIN. Its current personal-details DCI template
does not expose the required complete identity/provenance/canonical geography
contract for this receiving adapter. This is source inspection, not proof of a
specific deployed response. No Farmer or OpenG2P code was changed. Live lookup
and activation remain unaccepted until a source-owner contract proves the
selected FIN/FAN, required source identity/version/scope, signing and consent.
Unresolved intakes may still finalize and receive business approval; their live
activation stays held. Never configure FIN as UID or fill a contract gap from
notification values. The new `fin`/`fin_query_field` and `uid`/`uid_query_field`
record-contract settings map the two kinds separately; query defaults retain
`foundational_id` compatibility. The default FIN response path is also
`foundational_id`, so a source exposing separate FIN/FAN fields must configure
those paths explicitly.

## Verification and remaining acceptance

Run `python ci/refactor/verify_onboarding.py <registry> --build`, where registry
is `cropsown` or `livestock`. Prerequisites are Docker, Python, uv, and Postman
CLI 1.73+. The runner is portable outside the Protean workspace. It starts only
a labeled disposable PostgreSQL and local simulation, and removes both. It
records source hashes, native registration probes, local image IDs and a safe
traffic summary in `ONBOARDING_VERIFICATION.json`. Raw HTTP traffic is discarded;
synthetic signing keys exist only in memory. Run each target serially.

The PostgreSQL ledger suite exercises 20 concurrent submissions, duplicate
finalization/restart, rollback, reference reuse, pending/rejected dependencies,
source identity conflicts, duplicate/stale updates and UID rotation. The native
service suite uses actual pinned native models/CR/history/approval code in
temporary PostgreSQL schemas. Three cases exercise the real native Keycloak
and AWE HTTP clients against Postman: scoped ADD routing, UPDATE using old
geography with lost-response/idempotent recovery, and IAM failure with bounded
retries/visible diagnostics. These assert zero identity-grant writes. A separate
context test injects a delivery stand-in; callback tests call native service
methods directly. The HTTP suite
executes the real client against a signed local Postman stand-in based on the
official DCI envelope and explicit receiving trust requirements. The Postman
workspace search was unavailable because the CLI was signed out, so an owner's
published mock was not retrieved. Local simulation required no account or real
credential. These tests do not
certify the current Farmer template, real Consent Manager or real AWE tasks.
The runner executes 66 tests per registry (25 profile rules, eight projection,
14 PostgreSQL ledger, 12 native lifecycle/client and seven signed HTTP), plus
ordinary CI discovery with integrations skipped and packaged registration probes.

Live Kafka-created tasks, independent target decisions, IAM/geo access denial,
pending ADD portal access, native import/ODK/attachment parity, all domain rules
and schedules, authenticated dashboards, fresh application installation and
restored-database upgrades remain acceptance gates. Ordinary CI dependency-gates
the integration suites; run the explicit runner for integration evidence. Neither
packaging nor these isolated tests authorize promotion, develop replacement or
staging deployment.
