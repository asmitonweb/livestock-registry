# Livestock 1.2 source port

This branch is incomplete. `STATUS.json` lists unresolved delivery and acceptance
requirements. Helm refuses to render deployment manifests and the seed image
refuses to run seed SQL. No environment, existing geographic assignment, identity
grant, record, document, approval, connector checkpoint or legacy branch was
changed by this work.

The source base is OpenG2P Farmer `ed1e70ccf18df5476fa404ed8018e12d55e418fa`.
Domain source decisions and original revision are in `SOURCE_MAP.json`. Legacy
COSS develop is not merged. API and worker packages use the native selected
extension namespace/factory. Published platform image/chart version is
`1.2.2-rc.557`; UI source is pinned to its OCI revision
`4d5482dfea23d1accf98fe474fb90ed6ddb5f651`. Local image IDs and published base
digests are recorded in `IMAGES.json`; local images are not registry releases.

`VERIFICATION.json` records source, model/schema, migration-coverage, native
factory, portable rule, installed-wheel and image-build checks. Helm's render
gate is verified in `evidence/helm-render-gate.json`. These checks use isolated,
network-disabled probes, not live registry acceptance. The six image roles are
staff API, partner API, Celery, seed, staff UI and sanity tests. Building the
sanity image does not run its E2E suite. Custom widgets/forms still require
semantic metadata reconciliation and visual acceptance.

The reference integration follow-up is described in ONBOARDING.md and
ONBOARDING_VERIFICATION.json. Native CELERY_IMPORTS resolves the previously
reported worker-registration gap. Signed lookup, durable correlation, registered
native reference application and dependency release are implemented. Real
Farmer/Consent Manager, Kafka/AWE/portal behavior, remaining domain/metadata/job
parity and preservation acceptance still block promotion. The revised user contract accepts FIN12 or FAN16 in separate fields; a
signed response must prove the declared identifier. Unresolved lookups permit
business approval but keep native activation held (REVIEW_A_FIXES.md).

Retain existing Jenkins jobs/agents/credential references/ECR paths and deploy
helpers. Jenkins validates this source port; its deployment stages remain bound
to their existing develop/staging branches. Do not rename/replace develop,
archive it, force-push it, or deploy this branch until all acceptance gates pass.
Staging additionally requires a successful preservation rehearsal.

Runtime boundaries are Commons, Farmer, Crop and Livestock. Run acceptance
serially with Commons + Farmer + Crop, then Commons + Farmer + Livestock; at most
two registries running simultaneously. No target application stack was started
for these packaging checks.

Official contracts:
- https://github.com/OpenG2P/openg2p-documentation/blob/latest/products/registry/registry/developer-zone/building-a-registry/concepts/registry-extensions/extensions-contract.md
- https://github.com/OpenG2P/openg2p-documentation/blob/latest/products/registry/registry/design/awe-integration.md
- https://github.com/OpenG2P/openg2p-documentation/blob/latest/products/registry/registry/design/intake-forms/input-mechanisms.md

Confluence page 203653123 version 5 is now verified through the connected
Atlassian reader. The explicitly authorized plan narrows the broader proposed
profile to identity/contact/location and replaces legacy geographic attributes
with IAM/data-policy scope intersection. See PROFILE_V5.md for this follow-up.
