# FIN entered once, on eSignet

The user types FIN only on eSignet's UIN/VID screen. The registered widget opens
the provider directly; there is no custom FIN collection page or main-form FIN
input. The main form's FIN is a native read-only display filled from the verified
result. `faydaPopup.ts` was removed. Start accepts no FIN and ignores a browser
FIN value; a saved record's expected FIN is read server-side.

Core snapshots its expected FIN before token exchange, so a new intake needs a
small registered-extension bridge. The native adapter first validates token
signature, nonce and claims and obtains a 12-digit FIN. The extension binds that
subject to the native transaction, then delegates completion/persistence to
core. A request-local store/adapter wrapper replays that same transaction/token
response once: the authorization code is not exchanged twice, and no state is
put back into Redis. Native validation and SUCCESS-only claims storage remain.
Existing FINs still go through core's equality check; hand edits, invalid FINs,
record reuse and verification downgrade are refused. Client stamps are untrusted.

FIN12 remains separate from the receiving registries' existing UID/FAN16
reference contract. Their non-unique `fayda_fin` domain field avoids imposing
native `foundational_id` uniqueness on a person's multiple seasons/holdings.
The UI result retains `foundational_id` as its response key and writes to the
configured field. Approved reference demographics and business approvals remain
owned by the independent Farmer reference pipeline.

Checks: full native suites, native completion regression cases, metadata replay
twice with zero SQL errors, TypeScript and 25 production-widget browser checks.
These browser checks use deterministic handler fixtures. A real interactive
eSignet sign-in is the user's acceptance check; it is not claimed completed.
The canonical assertion-audience fix and its six regressions remain included.

The primary dirty Crop/Livestock experiments remain preserved. No compiled UI
patch, public mock-success endpoint or OpenG2P source change was introduced.
Receiving existing-database migrations still require restored-copy acceptance;
the source port does not automatically deploy these registries.

Native suite: **72 passed / 65 database-dependent tests skipped**.
Metadata seed replay twice: PASS, identical final metadata, no SQL errors.
TypeScript: PASS. Farmer is deployed locally first; receiving runtime is not
recreated by this port. Native provider and shared transaction-store configuration
remain environment-owned.
