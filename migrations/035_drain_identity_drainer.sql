-- Migration 035: switch drain service identity to healthspan.drainer@chitalkar.com
--
-- Old account: healthspan.drain@chitalkar.com  (UID 06ada8f7-76d7-49bf-90ef-93a9103b8b11)
-- New account: healthspan.drainer@chitalkar.com (UID resolved dynamically at apply time)
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/035_drain_identity_drainer.sql

DO $$
DECLARE
  old_uid  uuid := '06ada8f7-76d7-49bf-90ef-93a9103b8b11';
  new_uid  uuid;
  v_maint  boolean;
  v_pc     boolean;
  v_dea    boolean;
  v_dev    boolean;
BEGIN
  -- Resolve new UID by email
  SELECT id INTO new_uid FROM auth.users WHERE email = 'healthspan.drainer@chitalkar.com';
  IF new_uid IS NULL THEN
    RAISE EXCEPTION 'healthspan.drainer@chitalkar.com not found in auth.users — create the account first';
  END IF;

  RAISE NOTICE 'New drain UID: %', new_uid;

  -- Remove old drain memberships
  DELETE FROM public.family_memberships WHERE auth_user_id = old_uid;

  -- Insert new drain memberships (PC, Dea, Dev)
  INSERT INTO public.family_memberships (auth_user_id, profile_id, role)
  VALUES
    (new_uid, '21f69003-46f8-4e1c-a928-b1f694ce4aff', 'owner'),  -- PC
    (new_uid, '3eed5503-a26f-4b88-bb76-075208fa5de3', 'owner'),  -- Dea
    (new_uid, 'd4b7a3fe-6e7a-459f-935d-dfe4dbcfd2b1', 'owner')   -- Dev
  ON CONFLICT (auth_user_id, profile_id) DO NOTHING;

  -- Verify
  SELECT EXISTS (
    SELECT 1 FROM public.family_memberships fm
    JOIN public.profiles p ON p.id = fm.profile_id
    WHERE fm.auth_user_id = new_uid AND p.is_maintainer = true
  ) INTO v_maint;

  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = new_uid AND profile_id = '21f69003-46f8-4e1c-a928-b1f694ce4aff') INTO v_pc;
  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = new_uid AND profile_id = '3eed5503-a26f-4b88-bb76-075208fa5de3') INTO v_dea;
  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = new_uid AND profile_id = 'd4b7a3fe-6e7a-459f-935d-dfe4dbcfd2b1') INTO v_dev;

  ASSERT v_maint, 'is_maintainer(drainer) = false';
  ASSERT v_pc,    'has_profile_access(drainer, PC) = false';
  ASSERT v_dea,   'has_profile_access(drainer, Dea) = false';
  ASSERT v_dev,   'has_profile_access(drainer, Dev) = false';

  -- Confirm old drain is gone
  ASSERT NOT EXISTS (SELECT 1 FROM public.family_memberships WHERE auth_user_id = old_uid),
    'old drain memberships still present';

  RAISE NOTICE 'Migration 035 verify: OK — drainer is_maintainer=%, PC/Dea/Dev=%/%/%, old rows removed',
    v_maint, v_pc, v_dea, v_dev;
END $$;
