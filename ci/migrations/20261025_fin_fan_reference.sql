-- Explicit additive existing-database migration: run only on a restored copy first.
-- No existing identity, record, document, grant, approval or checkpoint is rewritten.
BEGIN;
ALTER TABLE IF EXISTS g2p_register_farmers ADD COLUMN IF NOT EXISTS fayda_fin varchar;
ALTER TABLE IF EXISTS g2p_register_history_farmers ADD COLUMN IF NOT EXISTS fayda_fin varchar;
ALTER TABLE IF EXISTS g2p_intake_form_farmers ADD COLUMN IF NOT EXISTS fayda_fin varchar;
ALTER TABLE IF EXISTS g2p_register_livestocks ADD COLUMN IF NOT EXISTS farmer_reference_status varchar;
ALTER TABLE IF EXISTS g2p_register_history_livestocks ADD COLUMN IF NOT EXISTS farmer_reference_status varchar;
ALTER TABLE IF EXISTS g2p_intake_form_livestocks ADD COLUMN IF NOT EXISTS farmer_reference_status varchar;
ALTER TABLE IF EXISTS coss_farmer_submission_dependencies ADD COLUMN IF NOT EXISTS identity_binding json;
CREATE TABLE IF NOT EXISTS coss_farmer_pending_lookups (
 submission_id uuid PRIMARY KEY, identity_json json NOT NULL,
 state varchar NOT NULL DEFAULT 'PENDING', attempts integer NOT NULL DEFAULT 0,
 retry_after timestamp, last_error_code varchar
);
CREATE INDEX IF NOT EXISTS ix_coss_farmer_pending_lookups_state ON coss_farmer_pending_lookups (state);
COMMIT;
