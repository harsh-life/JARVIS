# Character assets

The approved artwork and default presentation settings for JARVIS's
expressive presentation character (owner decisions **OD-CHAR-1** and
**OD-CHAR-2**, `docs/DECISION_REGISTER.md` §2M; how the pieces fit:
`docs/CHARACTER_PRESENTATION.md`).

- `presentation.json` — the default presentation settings
  (`shared/schemas/presentation.py` `CharacterPresentationSettings`):
  per-state activation thresholds, the confidence floor, transition, reduced
  motion and the timing/idle policy. Tune behaviour here, not in code.
- One directory per character profile; `default/` is the built-in profile.
  Each holds `profile.json` — the state → image mapping (`CharacterProfile`:
  `schema_version`, `profile_id`, `fallback`, `states`; a state the profile
  omits shows the fallback) — and the images, named by state.

The files in `default/` are the owner-supplied originals, byte for byte
(pinned by SHA-256 in `tests/presentation/test_character_profile.py`). They
are not repainted, cropped, re-encoded or resized; clients scale them at draw
time. Each file keeps the format it was supplied in, and its extension names
that format. All have transparent surroundings and the white sticker edge.

| # | State | File | Size (px) | Format |
|---|---|---|---|---|
| 1 | `happy` | `happy.webp` | 1071 × 1469 | WebP (lossy, alpha) |
| 2 | `satisfied` | `satisfied.png` | 843 × 1264 | PNG (RGBA) — replaced 2026-10-13 |
| 3 | `default` | `default.png` | 427 × 585 | PNG (RGBA) |
| 4 | `sleepy` | `sleepy.webp` | 1361 × 1156 | WebP (lossy, alpha) |
| 5 | `surprised` | `surprised.webp` | 843 × 1264 | WebP (lossy, alpha) — replaced 2026-10-13 |
| 6 | `angry` | `angry.webp` | 1145 × 1374 | WebP (lossy, alpha) |
| 7 | `sad` | `sad.webp` | 1145 × 1374 | WebP (lossy, alpha) |

The order is the owner's, and it is also the selection tie-break order.

The Android build bundles this directory into the APK's assets from here
(`android/app/build.gradle.kts`, `bundleCharacterAssets`); it is never copied
into `android/`.

An expression is presentation only. It is not a mood, a status, a result or a
permission: nothing that authorizes, executes, confirms, budgets or reports
status reads it.
