-- Review/rehearse on a restored copy before rollout; no data rewrites.
BEGIN;
ALTER TABLE public.g2p_register_livestocks ADD COLUMN IF NOT EXISTS fayda_fin varchar;
ALTER TABLE public.g2p_register_livestocks ADD COLUMN IF NOT EXISTS identity_verification_id varchar;
ALTER TABLE public.g2p_register_livestocks ADD COLUMN IF NOT EXISTS last_authentication_status varchar;
ALTER TABLE public.g2p_register_livestocks ADD COLUMN IF NOT EXISTS last_authenticated_on varchar;
ALTER TABLE public.g2p_register_livestocks ADD COLUMN IF NOT EXISTS authentication_expiry_date varchar;
ALTER TABLE public.g2p_register_history_livestocks ADD COLUMN IF NOT EXISTS fayda_fin varchar;
ALTER TABLE public.g2p_register_history_livestocks ADD COLUMN IF NOT EXISTS identity_verification_id varchar;
ALTER TABLE public.g2p_register_history_livestocks ADD COLUMN IF NOT EXISTS last_authentication_status varchar;
ALTER TABLE public.g2p_register_history_livestocks ADD COLUMN IF NOT EXISTS last_authenticated_on varchar;
ALTER TABLE public.g2p_register_history_livestocks ADD COLUMN IF NOT EXISTS authentication_expiry_date varchar;
ALTER TABLE public.g2p_intake_form_livestocks ADD COLUMN IF NOT EXISTS fayda_fin varchar;
ALTER TABLE public.g2p_intake_form_livestocks ADD COLUMN IF NOT EXISTS identity_verification_id varchar;
ALTER TABLE public.g2p_intake_form_livestocks ADD COLUMN IF NOT EXISTS last_authentication_status varchar;
ALTER TABLE public.g2p_intake_form_livestocks ADD COLUMN IF NOT EXISTS last_authenticated_on varchar;
ALTER TABLE public.g2p_intake_form_livestocks ADD COLUMN IF NOT EXISTS authentication_expiry_date varchar;
COMMIT;
