# Popup-only Fayda FIN verification

The registered `oan-identity-verification` widget collects FIN in its popup,
then navigates that same window to eSignet. There is no editable FIN input on
the domain form. Cancellation, timeout, failed start and terminal results clear
the busy state. The popup opener is removed before external navigation.

Core owns the native transaction, nonce/PKCE, token/subject checks, callback
and authentication row. The extension supplies the FIN context and checks
successful/unexpired evidence on saves. It refuses a mismatched subject,
cross-register/record reuse and removal of existing evidence to downgrade a
record. Missing/invalid FIN is rejected before provider lookup. The Next.js
start/result proxy routes and staff-only API routes are included.

FIN is 12 digits. It is stored as `fayda_fin` on Livestock register/history/intake
models and schemas, separately from UID/FAN16 (`fayda_fan_id`). This field is
non-unique because a person can have several domain holdings or crop seasons.
Approved central Farmer reference demographics and independent ADD/UPDATE
approval rules remain owned by the reference integration; this verification
does not grant approval or replace its identity pair.

## Configuration

Use this domain root register's active native eSignet provider and the native
`/registrant-auth/callback` URL. Request the adapter's FIN identity claim
(`individual_id` for the eSignet adapter); do not substitute an unrelated OIDC
subject or the UID/FAN field. Configure native shared transactions for multiple
API workers: `REGISTRY_CORE_REGISTRANT_AUTH_SESSION_STORE_BACKEND=redis` and
`REGISTRY_CORE_REGISTRANT_AUTH_REDIS_URL` through standard deployment Secrets.
Use the existing encryption/signing configuration; no keys or provider rows are
copied by this change. With no provider, start gives an actionable setup error.
There is no public mock-success endpoint or a replacement platform callback.


For a local HTTP gateway in front of an HTTPS canonical eSignet issuer, set
`provider_config.jwt_assertion_aud` to the exact canonical token endpoint from
discovery. The OAN registered service maps this native IAM audience setting
separately from the provider's transport `token_endpoint`. Do not change keys,
disable signature verification, or alter ID-token audiences to fix this mismatch.
Six regressions cover the override, native default and invalid configuration.

## Verification and rollout

- Native suite: **63 passed, 65 database-dependent tests skipped**.
- Native initialization/route/evidence-model registration probe: PASS.
- Complete native metadata replay twice: zero SQL errors; identical result;
  all existing section/form IDs retained. See new-dev/seed-rehearsal.json.
- Pinned-source UI assembly: TypeScript PASS. The production widget/helper
  passed 26 browser checks with deterministic request-handler fixtures.
- Complete Linux production UI image build: PASS (`oan/livestock-staff-ui:popup-only`).
- FIN value, invalid/blank inputs, native context, mismatch, expiration,
  reference UID preservation and evidence downgrade prevention are tested.

Evidence: workspace `Temp/fayda-popup-alignment/`. Those frontend/native checks
do not certify a live eSignet token exchange or resolve a client assertion
configuration error. No running application stack, provider credentials,
record, grant, approval or connector checkpoint was changed. Existing primary
checkout experimental edits remain preserved, separate from this native port.

Before updating an existing database, rehearse the additive evidence-column
migration beside this document on a restored copy. Promotion gates remain
active; the fresh metadata rehearsal is not existing-data preservation proof.
