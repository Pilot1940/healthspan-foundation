-- Migration 033: re-key drain service identity after account recreation
--
-- healthspan.drain@chitalkar.com was deleted + recreated, changing its auth.users id.
-- ON DELETE CASCADE removed the migration-032 family_memberships rows automatically.
-- This migration re-inserts them with the new uid.
--
-- Old uid: 1e2084cf-b1c4-484b-8448-33750c4d8e6c (deleted)
-- New uid: 06ada8f7-76d7-49bf-90ef-93a9103b8b11 (verified 2026-06-07)

INSERT INTO public.family_memberships (auth_user_id, profile_id, role)
VALUES
  ('06ada8f7-76d7-49bf-90ef-93a9103b8b11', '21f69003-46f8-4e1c-a928-b1f694ce4aff', 'owner'),
  ('06ada8f7-76d7-49bf-90ef-93a9103b8b11', '3eed5503-a26f-4b88-bb76-075208fa5de3', 'owner'),
  ('06ada8f7-76d7-49bf-90ef-93a9103b8b11', 'd4b7a3fe-6e7a-459f-935d-dfe4dbcfd2b1', 'owner')
ON CONFLICT (auth_user_id, profile_id) DO NOTHING;

DO $$
DECLARE
  drain_uid uuid := '06ada8f7-76d7-49bf-90ef-93a9103b8b11';
  v_maint   boolean;
  v_pc      boolean;
  v_dea     boolean;
  v_dev     boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM public.family_memberships fm
    JOIN public.profiles p ON p.id = fm.profile_id
    WHERE fm.auth_user_id = drain_uid AND p.is_maintainer = true
  ) INTO v_maint;

  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = drain_uid AND profile_id = '21f69003-46f8-4e1c-a928-b1f694ce4aff') INTO v_pc;
  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = drain_uid AND profile_id = '3eed5503-a26f-4b88-bb76-075208fa5de3') INTO v_dea;
  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = drain_uid AND profile_id = 'd4b7a3fe-6e7a-459f-935d-dfe4dbcfd2b1') INTO v_dev;

  ASSERT v_maint, 'is_maintainer(drain) = false';
  ASSERT v_pc,    'has_profile_access(drain, PC) = false';
  ASSERT v_dea,   'has_profile_access(drain, Dea) = false';
  ASSERT v_dev,   'has_profile_access(drain, Dev) = false';

  RAISE NOTICE 'Migration 033 verify: OK — drain is_maintainer=%, PC/Dea/Dev=%/%/%',
    v_maint, v_pc, v_dea, v_dev;
END $$;
