-- 054_media_inbox_logged_food_ids.sql
-- Reply-to-correct an AUTO-LOGGED food item was double-counting: the reply-to-clarify
-- loop only matched STAGED rows (clarify_message_id is stored only when staged), so a
-- reply to a confidently-logged item fell through to the normal "new log" path and the
-- original entry was never removed.
--
-- Fix (food only — supplements still have this gap, deferred): record the food_logs ids
-- an inserted food row created, so the telegram-webhook can DELETE the superseded entry
-- when the user replies to correct it. The drain also starts storing clarify_message_id
-- on inserted FOOD rows (not just staged ones) so the reply can be correlated.

ALTER TABLE public.media_inbox
  ADD COLUMN IF NOT EXISTS logged_food_ids uuid[];

COMMENT ON COLUMN public.media_inbox.logged_food_ids IS
  'food_logs ids this row wrote (inserted food only). Lets telegram-webhook supersede '
  '(delete + re-queue) the original entry when the user REPLIES to the "Logged" message '
  'to correct it — prevents double-counting. NULL for staged/text/supplement/brief rows.';

DO $$ BEGIN RAISE NOTICE '054: media_inbox.logged_food_ids added (supersede-on-reply for logged food)'; END $$;
