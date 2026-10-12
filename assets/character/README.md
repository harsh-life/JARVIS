# Character assets

The approved artwork for JARVIS's expressive presentation character
(owner decision **OD-CHAR-1**, `docs/DECISION_REGISTER.md` §2M). One
directory per character profile; `default/` is the built-in profile.

Each profile directory holds:

- `profile.json` — the state → image mapping (`shared/schemas/presentation.py`
  `CharacterProfile`): `schema_version`, `profile_id`, `fallback` (the state
  shown before anything is selected, for an unknown state, and when an image
  fails to load) and `states`. A state the profile omits shows the fallback.
- the images, named by state.

The files in `default/` are the owner-supplied originals, byte for byte
(pinned by SHA-256 in `tests/presentation/test_character_profile.py`). They
are not repainted, cropped, re-encoded or resized; clients scale them at draw
time. `default.png` is a PNG; the other six were supplied as WebP (lossy
colour with an alpha channel and an ICC profile) and keep that format and
extension, so no file is mislabelled and no pixel is changed.

| State | File | Size (px) |
|---|---|---|
| `default` | `default.png` | 427 × 585 |
| `satisfied` | `satisfied.webp` | 1024 × 1536 |
| `happy` | `happy.webp` | 1071 × 1469 |
| `sleepy` | `sleepy.webp` | 1361 × 1156 |
| `surprised` | `surprised.webp` | 1024 × 1536 |
| `angry` | `angry.webp` | 1145 × 1374 |
| `sad` | `sad.webp` | 1145 × 1374 |

The Android build bundles this directory into the APK's assets from here
(`android/app/build.gradle.kts`, `bundleCharacterAssets`); it is never copied
into `android/`.

An expression is presentation only. It is not a mood, a status, a result or a
permission: nothing that authorizes, executes, confirms, budgets or reports
status reads it.
