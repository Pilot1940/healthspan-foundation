-- 055_media_inbox_staged_review_ids.sql
-- When a STAGED food item is superseded by a clarification reply, its
-- stg_food_log_review row was left status='pending' — an orphan that lingers in the
-- maintainer review queue forever. stg_food_log_review has no back-link to media_inbox,
-- so the webhook couldn't find the review row to retire.
--
-- Fix: the drain records the stg_food_log_review id(s) it created when staging food, so
-- the telegram-webhook can mark them 'merged' when the user clarifies (supersedes) the
-- staged item. Mirrors logged_food_ids (mig 054) for the inserted-food supersede path.

ALTER TABLE public.media_inbox
  ADD COLUMN IF NOT EXISTS staged_review_ids uuid[];

COMMENT ON COLUMN public.media_inbox.staged_review_ids IS
  'stg_food_log_review ids this row STAGED (food only). Lets telegram-webhook retire '
  '(status->merged) the review row when the user REPLIES to clarify — otherwise the '
  'superseded staged extraction lingers as a phantom in the maintainer review queue.';

DO $$ BEGIN RAISE NOTICE '055: media_inbox.staged_review_ids added (retire review row on clarify-supersede)'; END $$;
