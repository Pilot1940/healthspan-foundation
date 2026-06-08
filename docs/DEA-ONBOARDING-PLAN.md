# Dea — Onboarding Plan

**Status (verified 2026-06-08): ~95% done. One action remains — the Telegram link.**

Dea is a minor (13F). The system already enforces minor-safe behaviour everywhere via
`is_minor` (growth/performance framing only; no deficit/restriction/hormone/fasting language;
maintainer-only machinery hidden). PC is her maintainer — he sees her data; she sees outcomes.

---

## Already in place (verified against the live DB + repo)

| Piece | State |
|---|---|
| **Profile** | `3eed5503-a26f-4b88-bb76-075208fa5de3` — "Dea Singh Chitalkar", `relationship='child'`, `sex='female'`, active |
| **Auth user** | `47501376-6adb-458e-8679-57a4a4176692` |
| **Family membership** | PC (`0b0e4093…`, owner) → Dea ✓ and Dea (`47501376…`, self) → Dea ✓ — PC manages, Dea owns her row |
| **WHOOP** | Linked (whoop_user `22399950`) + backfilled: 140 workouts, 300 cycles, 281 sleeps |
| **Config** | `config/dea.config.json` — `is_minor: true`, profile_id, direct_role conn, WHOOP creds |
| **Context MD** | `context/dea.context.md` (72 lines) — coaching voice / norms / minor-safety |
| **Ingestion secret** | `config/dea.secret.txt` |

Because `profiles.relationship = 'child'`, when Dea links, `telegram-webhook` sets her
`telegram_identities.is_minor = true` automatically (index.ts:251) — so minor-safe framing
applies from her first message. No manual flag needed.

---

## The one remaining step — Telegram link

Dea has **no `telegram_identities` row**, so she can't use the bot yet. To finish:

1. **Mint a single-use link code** (PC runs, from repo root):
   ```bash
   python scripts/hs_ops.py mint-link-code 3eed5503-a26f-4b88-bb76-075208fa5de3
   ```
2. **On Dea's own phone/Telegram**, open the **Chitalkar Family Health Tracker** bot and send the
   code (or `/start <code>`).
3. The bot creates her `telegram_identities` row with `is_minor = true` (from `relationship='child'`)
   and replies **"✅ Account linked."**

That's it — she's live. Her photos/text flow through the same LLM-routed drain as PC's, with
minor-safe wording.

---

## Verification after linking

- Dea sends `took my vitamin` or a meal photo → confirmation uses **growth/positive** wording
  ("📊 … tracked. Nice! 💪"), never deficit/restriction language.
- Dea sends `how am I doing?` → brief with **growth/performance framing**, no calorie-deficit line,
  no Viome restriction section (minors skip it).
- PC (maintainer) can see Dea's data and the staging/review machinery; Dea cannot.
- WHOOP section populates from her already-synced data; refresh-on-interaction keeps it current.

---

## Optional / nice-to-have (not blockers)

- `profiles.date_of_birth` is NULL for Dea — `is_minor` derives from `relationship='child'`, so not
  required, but setting her DOB enables any future age-based logic.
- Confirm `context/dea.context.md` reflects her current goals/voice (it exists; review the content
  with PC before she starts so the coaching tone is right for a 13-year-old).
- Decide whether Dea logs on her own device or PC relays — the link ties one Telegram `chat_id` to
  her profile, so it should be **her** Telegram account.

---

## Why this is safe to do now

Every minor-safety guarantee is already coded and tested for PC's account in maintainer mode:
`compose_confirmation` / `compose_brief` branch on `is_minor`; the drain says "I'll check this one
with PC" for a minor's staged item; RLS hides `query_audit` / `stg_*_review` from non-maintainers.
Linking Dea only activates a path that already exists — it doesn't introduce new surface.
