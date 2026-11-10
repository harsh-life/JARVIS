# Proposal: a pixel-art JARVIS desktop companion (Linux, macOS, Windows)

**Status: proposal only. Nothing here is built.** It is a separate, later PR from
Character Activity v1 (OD-CHAR-4). It reuses that work's contract and semantics
unchanged, and it needs three owner decisions before code starts (§9).

## 1. What it is

A small pixel-art JARVIS that sits on the desktop, above other windows, and
presents the same seven expressions the Android character does: happy,
satisfied, default, sleepy, surprised, angry, sad. They mean the same things
(`docs/CHARACTER_PRESENTATION.md`): JARVIS's operational activity, never a
feeling and never anger at the user. The companion shows; it does not act. It
never approves, confirms or submits anything, the same rule as the Android
overlay (docs/23 §7). A click opens JARVIS; it never answers a pending action.

## 2. What it reuses, unchanged

| Piece | Reused as |
|---|---|
| `GET /api/v1/presentation/activity` | the only data it reads, polled while visible |
| `ActivityPresentation` | parsed and validated whole; held to `shared/android/presentation_activity_samples.json` |
| `ActivityPolicy` | a third implementation (TypeScript, §4), held to `shared/android/presentation_activity_policy_vectors.json` in CI |
| `CharacterPresentationSettings` | the same document and defaults (`assets/character/presentation.json`), optionally fetched from `/config/presentation` |
| `presentation_intensity` | the same 1–10 mapping, held to `presentation_intensity_vectors.json` |
| Boundaries | import/dependency rule: the companion's character code reads only the report and local input events, and nothing that holds a credential reads an expression (mirrors `CharacterBoundaryTest`) |

No server change is needed for presentation. The report is already per user,
so a task started on the phone shows on the desktop too (§8).

## 3. Shell: Tauri 2 (recommended)

| Option | Fit | Why not |
|---|---|---|
| **Tauri 2** (Rust + system webview) | small binary (~5–15 MB); transparent, undecorated, always-on-top windows; tray; click-through; Rust core for the keychain and HTTP; a webview canvas for the sprite renderer; same JS as the planned web client | relies on each OS's webview (WebKitGTK on Linux has the most variance) |
| Electron | most predictable rendering everywhere | ~150 MB and a bundled Chromium for a 128 px sprite; a larger attack surface |
| Native per OS (Swift / WinUI / GTK) | best OS integration | three codebases and three policy implementations |
| Flutter desktop | one codebase | transparent always-on-top windows are plugin-dependent; no shared code with the web client |

Window configuration (per OS caveats in §6):

```jsonc
// tauri.conf.json → app.windows[0]
{ "label": "companion", "width": 160, "height": 160,
  "transparent": true, "decorations": false, "shadow": false,
  "alwaysOnTop": true, "skipTaskbar": true, "resizable": false,
  "focus": false, "visibleOnAllWorkspaces": true }
```

Plugins: `tray-icon` (core feature), `tauri-plugin-positioner` (snap to tray
or screen edges), `tauri-plugin-autostart` (off by default),
`tauri-plugin-single-instance`, `tauri-plugin-updater` (signed updates).
Credentials: the `keyring` crate (macOS Keychain, Windows Credential Manager,
Linux Secret Service) from Rust. The token never reaches the webview: Rust makes
the HTTPS request and passes only the report text over IPC. The capability
file allows the webview exactly two commands, `activity_report` and
`open_jarvis`, and nothing else (no shell, no filesystem, no HTTP).

## 4. Architecture

```
 Rust core (src-tauri)                          webview (ES modules, no framework)
 ─────────────────────                          ─────────────────────────────────
 keychain token ─▶ reqwest GET /activity ─IPC─▶ ActivityReport.parse ─▶ ActivityPolicy ─▶ SpriteRenderer
 tray menu, window, autostart, OS events ─IPC─▶ local activity (click, wake from sleep)        (canvas)
```

- **Policy in TypeScript, not Rust.** The web client (`web/`, plain ES modules)
  will need the same policy for its character page (pilot C11). One JS
  implementation serves both, as `shared/web/activity-policy.js` beside the
  vectors, tested with Node's built-in test runner against the same JSON.
  That makes three implementations in all: Python reference, Kotlin, JS.
- **Polling:** every `activity.poll_interval_ms` (15 s) while the window is
  visible and the session is unlocked. It stops when hidden, when the screen
  is locked, or after `idle_timeout_ms` of system idle. The last report then
  goes stale and the character falls to its baseline.
- **Local activity** is clicking the companion, opening JARVIS from it, or the
  machine waking from sleep. These are events only, never keystrokes or window
  titles. The companion installs no global keyboard or mouse hooks.

## 5. Sprite-sheet rendering

- **Art:** a new pixel-art set of the seven expressions, drawn to the approved
  character's design. OD-CHAR-3 permits UI-only artwork, but the owner approves
  the art itself (§9). Proposed sizes: a 32×32 base drawn at integer scales 3×,
  4× and 5× (96–160 px), one PNG atlas plus a JSON frame index (Aseprite or
  TexturePacker export), pinned by SHA-256 like the Android assets.
- **Animations per expression:** `idle_<state>` (4–8 frames), `to_<state>`
  (2–4 frames), and `long_idle_<state>` (once, after ten minutes, as on
  Android). Missing animations fall back to `idle_default`; a missing atlas
  falls back to a static `default` frame.
- **Intensity, 1–10:** pixel art cannot scale smoothly, so intensity selects a
  band. 1–3 plays the `low` variant at 0.75× frame rate; 4–7 plays the
  canonical animation at 1×; 8–10 plays the `high` variant (bigger poses) at
  1.25×. The dead band from §3a already prevents flapping between bands.
- **Renderer:** a 2D canvas with `imageSmoothingEnabled = false`, integer
  scaling only, and frames advanced from `requestAnimationFrame` against a
  fixed timestep. Nothing is drawn while hidden. With reduced motion (the
  OS's `prefers-reduced-motion`, or `reduced_motion: always`) it shows the
  state's first idle frame, still, with no transitions.

## 6. Per-OS behaviour and limits

| | Windows 10/11 | macOS 12+ | Linux X11 | Linux Wayland |
|---|---|---|---|---|
| Webview | WebView2 (Evergreen; bootstrapper for Win10 without it) | WKWebView | WebKitGTK 4.1 | WebKitGTK 4.1 |
| Transparent window | yes | yes, **only with `macOSPrivateApi`**, which risks rejection from the Mac App Store, so distribute outside it (signed and notarized DMG) | yes, **if a compositor runs**; without one, a black box, so fall back to a rectangular window | yes (compositors always composite) |
| Always on top | yes; still covered by exclusive-fullscreen apps | yes (floating level); over fullscreen Spaces needs `visibleOnAllWorkspaces` | yes (EWMH `_NET_WM_STATE_ABOVE`; window-manager dependent) | **no standard protocol**: GNOME ignores it; KDE honours window rules; wlroots compositors support `wlr-layer-shell` (needs a GTK layer-shell path, a spike) |
| Positioning / drag | yes | yes | yes | **the app cannot set or read global position**: `startDragging()` works (the compositor moves it), but "remember position" and "snap to corner" do not |
| Click-through (`set_ignore_cursor_events`) | yes | yes | yes (input shape) | works via an empty input region on most compositors, but needs verifying per compositor (a spike); the window then cannot see the pointer to switch it back, so toggle from the tray |
| Tray | yes | menu-bar item | needs StatusNotifier/AppIndicator (`libayatana-appindicator`); **GNOME shows none without an extension**, so fall back to a small "open menu" button on the companion | same as X11 |
| No Dock/taskbar entry | `skipTaskbar` | `ActivationPolicy::Accessory` | `skipTaskbar` (WM dependent) | compositor dependent |
| Known issue | — | release DMGs reported rendering opaque in some Tauri versions (test the bundle, not just `tauri dev`) | — | some NVIDIA + WebKitGTK setups render blank; document `WEBKIT_DISABLE_DMABUF_RENDERER=1` |

**Linux policy.** Detect the session (`XDG_SESSION_TYPE`) and degrade openly.
On Wayland without layer-shell, the companion is a normal small window: it is
not pinned on top, no position is remembered, and the settings page says so.
`GDK_BACKEND=x11` (XWayland) restores the X11 behaviour where available; offer
it as a documented opt-in, not a default.

## 7. Interaction behaviour

| Input | Does |
|---|---|
| Drag | moves the companion (on Wayland, the compositor moves it) |
| Single click | local activity (wakes `sleepy`) and a small speech-free bubble with the **status line's** words, read from the same status data as the app, not from the character |
| Double click / tray "Open JARVIS" | opens the JARVIS web client (or focuses it) |
| Tray menu | Show/Hide · Click-through on/off · Size (3×/4×/5×) · Reduced motion · Start at login (off) · Sign out · Quit |
| Hover | nothing beyond a cursor change: no pop-ups over the user's work |
| Fullscreen app in front | hide, then return when it leaves (Windows/macOS; best effort on X11; not available on Wayland) |
| Locked or asleep | stop polling; on wake, local activity and one fresh request |

Never: approve, confirm, cancel or submit a task; read the clipboard, screen,
keystrokes or other windows; play sound; speak.

## 8. Privacy and security

- The token lives in the OS keychain, and only Rust reads it. The webview
  sees only report text.
- The companion stores only settings: size, position where available, and
  click-through. It never stores reports, scores or expressions, and never
  logs them.
- The report is per user. Opening the companion on a shared computer shows
  that user's activity, so "Sign out" clears the token at once.
- **Device identity is a dependency.** The server registers only Android
  devices (`DevicePlatform` has `android` alone; others are `[FUTURE]`). The
  companion needs either a `desktop` device platform (a `01` enum change,
  device credential and revocation included) or browser sign-in (M3, not
  built) with a refresh-token flow. See §9.

## 9. Decisions needed before building

1. **Device identity for desktop:** add `DevicePlatform.desktop` with an
   Ed25519 device credential (recommended: same revocation story as phones), or
   reuse M3 browser sessions.
2. **Pixel-art artwork approval:** the seven sprites (and their low/high
   variants) as the desktop rendering of the approved character.
3. **Distribution:** signed and notarized DMG outside the Mac App Store (needed
   for transparency), MSI/MSIX, and AppImage + .deb (Flatpak later; its
   sandbox needs a Secret Service portal check).

## 10. Plan and estimate

| Step | Content | Exit |
|---|---|---|
| D0 spike (2–3 days) | Tauri 2 window matrix on Win11, macOS 14, Ubuntu X11, GNOME Wayland, KDE Wayland, Sway: transparency, on-top, click-through, tray, bundle rendering | a filled-in §6 table, with screenshots |
| D1 | `shared/web/activity-policy.js` + parser, held to the shared vectors (also unblocks the web character page) | Node tests green on the vectors |
| D2 | Tauri shell, keychain, the `activity_report` command, polling lifecycle, tray | an end-to-end report reaches the policy on all three OSes |
| D3 | sprite renderer, intensity bands, reduced motion, placeholder art | renderer tests (frame selection, band mapping) |
| D4 | final art, signing, notarization, updater, packaging | installable builds; a boundary test (the webview has no token, no shell, no filesystem) |

## Sources consulted

- Tauri 2 / tao release notes (`Window::set_ignore_cursor_events`): https://v2.tauri.app/release/tao/v0.30.3/
- Tauri issue on cursor events and ignored windows: https://github.com/tauri-apps/tauri/issues/11461
- macOS transparency requires `macOSPrivateApi` (App Store warning); release-build transparency issue: https://github.com/tauri-apps/tauri/issues/13415
- GNOME on programmatic always-on-top under Wayland: https://discourse.gnome.org/t/any-way-to-set-window-always-on-top-programmatically/31579 and https://discourse.gnome.org/t/gtk4-and-set-keep-above-and-other-similar-apis-that-used-to-be-tied-to-x11/12447

The per-compositor click-through and layer-shell rows are unverified until the
D0 spike.
