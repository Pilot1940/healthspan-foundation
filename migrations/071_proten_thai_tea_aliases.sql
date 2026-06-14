-- Migration 071: add missing aliases to the global "Protein Thai Tea Shake" (Proten) reference
--
-- lookup_food_reference matches by EXACT lower(name)/alias. PC typed "Proten Thai Tea Protein
-- Shake" (and the misspelling "Protien"), neither of which was an alias on the global row
-- (mig 064), so it fell through to "needs calories" instead of resolving to 190 kcal / 35g P.
-- Append the exact phrasing + common misspellings. DISTINCT-merge keeps it idempotent.
-- (Deeper fix = fuzzy match / auto-promote — BACKLOG #25/#13.)

UPDATE public.food_reference
SET aliases = (
      SELECT array_agg(DISTINCT a ORDER BY a)
      FROM unnest(
        coalesce(aliases, ARRAY[]::text[]) || ARRAY[
          'proten thai tea protein shake',
          'proten thai tea protien shake',
          'protein thai tea shake',
          'thai tea protien shake',
          'proten thai tea protein',
          'proten protien shake'
        ]
      ) AS a
    ),
    updated_at = now()
WHERE lower(name) = 'protein thai tea shake'
  AND profile_id IS NULL;
