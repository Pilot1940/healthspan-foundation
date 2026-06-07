-- Migration 034: drain vision model config
--
-- Adds drain.vision_model to system_config so the --once loop reads the model
-- from the DB rather than hardcoding it. Default: claude-sonnet-4-6.
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/034_drain_vision_config.sql

INSERT INTO public.system_config (key, value, description, category, is_active, updated_at)
VALUES (
  'drain.vision_model',
  '"claude-sonnet-4-6"',
  'Anthropic model used by inbox_drain --once for vision extraction. '
  'Must be a vision-capable Claude model.',
  'drain', true, now()
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE v_count int;
BEGIN
  SELECT COUNT(*) INTO v_count FROM system_config
  WHERE key = 'drain.vision_model' AND is_active = true;
  ASSERT v_count = 1, 'drain.vision_model config row missing';
  RAISE NOTICE 'Migration 034 verify: OK (drain.vision_model ✓)';
END $$;
