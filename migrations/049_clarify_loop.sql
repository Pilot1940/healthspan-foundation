-- =============================================================
-- 049_clarify_loop.sql
-- Reply-to-clarify loop: when an item stages, the bot sends an LLM-written "what was
-- unclear + what to send" message and stores THAT message's Telegram id here. When the
-- user replies to it, telegram-webhook correlates the reply to this row, appends the
-- clarification to the caption, and re-queues it (status → pending) so the drain
-- re-extracts with the new context. The LLM manages both ends (the question and the
-- re-extraction).
--
-- Additive only (ADD COLUMN IF NOT EXISTS) — no data migration, no RLS change
-- (media_inbox already has its policies; adding columns inherits them).
-- =============================================================
BEGIN;

ALTER TABLE public.media_inbox
  ADD COLUMN IF NOT EXISTS clarify_message_id BIGINT,
  ADD COLUMN IF NOT EXISTS clarify_count SMALLINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.media_inbox.clarify_message_id IS
  'Telegram message_id of the bot''s descriptive review message; a user reply to it is correlated back to this row for re-processing.';
COMMENT ON COLUMN public.media_inbox.clarify_count IS
  'How many clarification rounds this item has been through (cap prevents loops).';

-- Index the correlation key (only staged rows awaiting a reply carry one).
CREATE INDEX IF NOT EXISTS idx_media_inbox_clarify_message_id
  ON public.media_inbox (clarify_message_id)
  WHERE clarify_message_id IS NOT NULL;

COMMIT;
