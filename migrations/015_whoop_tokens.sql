-- =============================================================
-- 015_whoop_tokens.sql  (TABLE — per-profile WHOOP OAuth tokens)
-- PROMPT 12. Stores the offline-scope refresh token (+ current access token) per
-- profile, written/read ONLY by the Edge functions via service_role.
--
-- SECURITY: these are live credentials. RLS is ENABLED with NO policy for
-- authenticated/anon/healthspan_app → those roles see NOTHING (RLS default-deny).
-- All table privileges are revoked from them too. The Edge functions use the
-- service_role key, which BYPASSES RLS — that is the only access path.
-- =============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS public.whoop_tokens (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    uuid NOT NULL UNIQUE REFERENCES public.profiles(id),
  access_token  text,
  refresh_token text,
  expires_at    timestamptz,
  scope         text,
  whoop_user_id text,                      -- WHOOP's numeric user id, for webhook user_id → profile mapping
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_whoop_tokens_whoop_user ON public.whoop_tokens(whoop_user_id);

ALTER TABLE public.whoop_tokens ENABLE ROW LEVEL SECURITY;
-- intentionally NO policy for authenticated/anon → default-deny for the skill roles.

-- Lock out every non-service role at the grant level too (belt and braces).
REVOKE ALL PRIVILEGES ON public.whoop_tokens FROM authenticated, anon;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='healthspan_app') THEN
    EXECUTE 'REVOKE ALL PRIVILEGES ON public.whoop_tokens FROM healthspan_app';
  END IF;
END $$;

DO $$ BEGIN RAISE NOTICE '015: whoop_tokens created (service_role-only; RLS default-deny).'; END $$;
COMMIT;
