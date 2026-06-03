-- =============================================================
-- 023_fix_is_maintainer_via_memberships.sql  (FIX — redefine is_maintainer)
-- 022 defined is_maintainer() against profiles.auth_user_id. But a profile can have
-- MULTIPLE family_memberships (PC has role 'owner' AND role 'self', each a different
-- auth_user_id), and the SKILL authenticates as the 'self' membership — which is NOT
-- profiles.auth_user_id. So is_maintainer() returned false for PC's real skill
-- connection, and the maintainer-only RLS would have hidden the maintenance tables
-- from PC too (and ingest_health reported maintainer=false for PC).
--
-- Fix: resolve maintainer through family_memberships, EXACTLY like has_profile_access
-- resolves access — auth.uid() → profile → profiles.is_maintainer. Now ANY of a
-- maintainer's auth identities is recognised; a non-maintainer (Dea) stays false.
-- =============================================================
BEGIN;

CREATE OR REPLACE FUNCTION public.is_maintainer(p_uid uuid DEFAULT NULL)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $function$
  SELECT EXISTS (
    SELECT 1
    FROM family_memberships fm
    JOIN profiles p ON p.id = fm.profile_id
    WHERE fm.auth_user_id = COALESCE(p_uid, auth.uid())
      AND p.is_maintainer = true
  );
$function$;

DO $$ BEGIN RAISE NOTICE '023: is_maintainer() now resolves via family_memberships (mirrors has_profile_access)'; END $$;

COMMIT;
