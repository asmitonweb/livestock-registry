# Optional Fayda verification and manual fallback

"Verify with Fayda" opens eSignet directly. The verified path asks for FIN only
on eSignet's UIN/VID screen; no custom FIN page exists. On SUCCESS the verified
12-digit FIN fills the configured form attribute and native metadata disables
its input. Server checks refuse changes, clearing, forged stamps and proof reuse.

When verification is unavailable, the manual ID remains editable and is stored
unverified with empty authentication stamps. Verification is optional at finalize;
normal intake/change-request approval remains the activation control. A failed or
unfinished-expired attempt may be discarded for a valid manual ID on a never-verified
record. A previously successful verification cannot be removed to bypass its lock.
Document upload remains the evidence mechanism; this change adds no approval bypass.

Farmer's combined `foundational_id` accepts FIN12 or FAN16. Crop Sown/Livestock
retain separate FIN12 (`fayda_fin`) and existing UID/FAN16 (`fayda_fan_id`) fields;
verification never manufactures a FAN or alters authoritative Farmer projections.
A typed 12-digit FIN supplied to Verify must match the verified subject. It is
carried as a request hint (`oan_form_fin`), never as a trusted expected identity;
a FAN16 cannot constrain the FIN. A mismatch names only the typed value and never
discloses another signed-in identity. A saved SUCCESS identity remains the expected
subject, including a completion-time race check.

Core snapshots expected FIN before token exchange. The OAN extension prevalidates
the new subject with the native adapter, binds exactly 12 ASCII digits, then
delegates completion/storage to core using request-local transaction/token replay.
The single-use code is exchanged once; state is never reinserted into Redis.
Native signature/nonce/claim checks and SUCCESS-only claim storage remain.

`provider_config.token_timeout_seconds` defaults to 30 positive finite seconds.
iam_core 1.4.2 constructs its OAuth client without a timeout (httpx defaults to 5 s).
The OAN call-scoped bridge runs the unchanged native exchange with an isolated
globals dictionary and a timeout-aware, call-owned client factory. No shared IAM
module/class/method is replaced, and the client is closed. Startup checks the
native factory assumption: a future incompatible IAM upgrade logs one versioned
warning and falls back to native behavior. CI checks that assumption explicitly.
Timeouts explain the duration, retry action and `token_timeout_seconds` setting.
The canonical assertion-audience fix is preserved independently of transport.

2026-10-25 verification: Farmer 118 native tests PASS; Crop Sown and Livestock
98 PASS and 65 database-dependent skips each. Complete metadata replay twice on
disposable/owned scratch databases PASS for all three, zero SQL errors, identical
metadata. Earlier unchanged widget checks: 25 browser checks and all three tsc PASS.
Real HTTP Postman simulation reproduced native ReadTimeout at 5.13 s, completed
OAN's 30 s case at 7.55 s and explained a configured 1 s timeout at 1.02 s. Its
stand-in delays the documented OAuth token response 7.5 s; the actual OAN/native
client runs. This proves transport timeout handling, not a live identity sign-in.

Only the local Farmer API was recreated for this follow-up. Its stable Identity
section was updated in place to editable FIN/FAN plus conditional SUCCESS lock;
the selected register's Verify-enable flag is true. No live
schema migration, seed replay, domain record, identity-grant or checkpoint edit.
Live assertion signature/audience still match discovery. Fresh user sign-in is
pending. Receiving images are local build artifacts; neither registry is deployed.
Existing-database receiving migrations still require restored-copy acceptance.

Deployment: set `requires_registrant_authentication=true` on the root register
to enable native Verify. Despite its name, this flag is not read by finalize;
verification remains optional and approval controls activation. Setting false
disables the native start operation. SQL assertions keep the action enabled.
The first fallback commit incorrectly cleared the flag; the lead restored the
live Farmer setting, and this correction supersedes that source configuration.

Provider configuration: request `claims_locales: "en am"` alongside the existing
claims mapping. Mock identity 0.9.3 maps `am` to `amh` and can emit `name#am`.
Receiving registries bind only the verified FIN; identity-provider demographic
claims never replace the signed central Farmer profile or its target approval.
Provider settings belong to the registry-owned provider/Helm configuration and
are not overwritten by recurring registry metadata seed.

Each registry database also needs its own active
`g2p_registrant_authentication_providers` row. Livestock's row must target
register `997676d3-7008-59f9-b23e-613ad79bbb08`. Its local callback is
`http://staff-livestock-registry.openg2p.localtest.me/registrant-auth/callback`,
and that exact callback must also be authorized in the local eSignet client
`oan-registry-intake`. A working Farmer or Crop provider does not configure
Livestock. Provision this through secure deployment administration when the
Livestock runtime is next started; it is deployment data, not recurring seed.
Preserve other callback entries and keep all private settings out of Git/logs.
See `OAN/oan-helm/docs/registry-esignet-onboarding.md` for the per-registry
deployment/read-back checklist. No Livestock provider row has been provisioned
by this source-only change.

The verification footer portals all messages, status and the button into the
native action area. `useLayoutEffect` finds the area before anything is shown,
preventing an inline flash. Messages take the left flexible space while the status
and button stay together. The component is ported from Farmer b553762 without
changing generated platform assets.

Extension migration runs native `create_migrate` first, then adds every missing
mapped column to an existing extension table, logging each addition at DEBUG.
It never changes existing types, constraints, defaults, unknown columns or values.
New columns on existing tables are nullable with no fabricated backfill; native
creation owns new tables. The three reviewed SQL artifacts remain as audit and
manual-review material, not an automatically repeated data correction. Real COSS
restored-environment preservation remains a separate promotion gate.
