# 24_ENDPOINT_ARCHITECTURE_DESKTOP_ASSESSMENT.md
## JARVIS / Hypermind Track B — Multi-Endpoint Architecture & Desktop Client Assessment

**Package:** research / architecture assessment · **Written:** 2026-09-28
**Status:** **`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`**. Nothing here changes the runtime, authorization, identity, Android, or execution architecture. Where a proposal would touch existing contracts, §19 lists it as a follow-up for owner approval.
**Numbering:** `24` is an unreferenced free slot (verified in `26` §1.1).
**Audited against:** `harsh-life/JARVIS` `main` @ `4223c5f` plus the next-build documents `18`–`23`, `27`, `28`.

**Labels used in this document**

| Label | Meaning |
|---|---|
| `[LOCKED]` | existing canonical architecture, restated with its source; not open |
| `[RESEARCH]` | a finding from current external sources (listed in §22) |
| `[PROPOSED]` | this document's recommendation, pending ratification |
| `[OPEN — OWNER]` | a decision only the owner can make |
| `[FUTURE]` | considered architecturally, not an implementation target |

---

## 1. Executive summary

**The core answer.** A desktop client can be added without changing the core. It is another **endpoint**: it logs in through the same Google OIDC → internal `user_id` → device registration → device credential → short-lived session chain; it talks to the same gateway over HTTPS; it hosts adapters only for operations the server has already authorized. The runtime never learns anything new about "desktop" beyond an extra `endpoint_class` value and, later, extra platform adapters.

**Recommended candidate** `[PROPOSED]`:
- **Tauri 2** shell with a small **Rust "endpoint core"** and a web-technology UI.
- The Rust core holds the device key, runs login and the server channel, and hosts adapters.
- The web UI never touches the credential.
- Compose Multiplatform is the documented alternative if the owner values Kotlin code-sharing with Android over footprint (§4.3).

**First release should be a *communication endpoint* only:**
- tray presence, a global hotkey, text input, task status, notifications, and confirmations;
- voice after that;
- **zero execution adapters** in release 1.

Desktop execution (file access in folders the user grants, app launch, screen perception) comes later, one capability at a time. Keyboard/mouse injection and shell are not planned.

**Platforms:**
- Windows and macOS are first-class from the start.
- Linux is **supported, second tier**, starting with Ubuntu LTS `.deb` builds, because Wayland global shortcuts, tray icons and secret storage are uneven across desktops (§8).

**The generic endpoint model** `[PROPOSED]` is identity + session + transport + declared I/O and adapter profile, with authority always server-side. It fits phones, desktops, and future devices:
- earbuds attach **through** a parent endpoint;
- constrained IoT devices enroll through a pairing flow;
- none of them ever holds authority (§16–§17).

**Five things block implementation** (§20):
1. the desktop framework;
2. whether to add P-256 device credentials so desktop keys can be hardware-bound;
3. a Windows code-signing route;
4. the desktop speech-to-text placement;
5. whether cross-device confirmation is allowed.

---

## 2. Current architecture impact

### 2.1 What stays exactly as it is `[LOCKED]`

| Area | Source | Why desktop needs no change here |
|---|---|---|
| Identity derivation | `03`, PHONE-003 | the server derives the principal from the credential; a desktop asserts nothing |
| Five-dimension authorization | `04` | desktop requests go through the same engine as the principal |
| Capability registry, tiers, floor, confirmation tokens | `07`, `CAPABILITY_MATRIX.md` | a desktop adapter is gated by an existing or new registry entry like any other |
| SecretStore | `12`, OD-D1 | the client never holds a server secret (REPO-T2 applies to any client) |
| Runtime, recovery, breaker | `05`, `18` | endpoints are targets and UI surfaces, never runtimes |
| MemoryProvider / Vault | `11`, `21` | unchanged |
| Android trust model | `08`, `23` | unchanged; Android remains authoritative for its own adapters |
| OD-A1 residual | register §1 | unchanged; a new client adds no server-side isolation claim |

### 2.2 What the current code already assumes that desktop touches

These are the real friction points, found in the code:

1. **The login callback returns JSON.** `server/gateway/routers/auth.py` `/auth/oidc/callback` returns a bootstrap response to whoever loaded it. A native app needs the result delivered back to itself. This is the same gap `23` §3 already found for Android.
2. **Device credentials are Ed25519 only.** `server/auth/device.py` verifies Ed25519 proofs.
   - `[RESEARCH-adjacent, verify at implementation]` The common desktop hardware key stores are the macOS Secure Enclave and TPM-backed Windows keys. As far as their documented key types go, both centre on NIST P-256 rather than Ed25519.
   - So a desktop Ed25519 key can be kept in the OS credential store, but it cannot be **hardware-bound** there (§9.3).
3. **`ExecutionPlatform.LINUX` already means the server host.** In `shared/schemas/agent.py`, `linux` is the platform for `system.restricted` running on the JARVIS server. A Linux *desktop endpoint* must not reuse that value, or a desktop operation could be routed to the server adapter.
4. **Device operations go to the authorizing device only.** `DeviceOperation.device_id` must be "the authorizing principal's own" (OD-DEV-1, `server/execution/android.py`). A task started on the laptop cannot act on the phone. This is correct and safe; changing it is an authorization change and is out of scope (§10.4).
5. **Confirmation tokens bind to the session.** `server/capabilities/confirmation.py` binds a token to principal *and session*. A task started on the phone can therefore only be confirmed from the phone's session. Whether the laptop may confirm it is a product decision (§10.5).

---

## 3. Desktop endpoint requirements

| Requirement | Release 1 | Later |
|---|---|---|
| Always-available: runs in tray/menu bar, starts at login (optional) | yes | |
| Global hotkey to summon a floating input panel | yes (with Linux fallback, §8) | |
| Text input → `POST /agent/tasks` (mode `execute` or `draft`, `18` §3) | yes | |
| Live task status over the endpoint channel | yes | |
| Native notifications (task done, confirmation needed, reminder from `22`) | yes | |
| Confirmation screens rendered from the server's canonical action description (`23` §5.4) | yes | |
| Step-up for `high_irreversible` via OS user presence or re-login | yes | |
| Voice input/output (`27`) | | release 2 |
| Execution adapters (files, apps, perception) | | release 3+ |
| Presentation layer / character | | deferred (`23` §7 hook reused) |
| Works against local VM, laptop-hosted, cloud VPS, multi-user deployments | yes (config: server base URL) | |

---

## 4. Technology comparison

### 4.1 Candidates

| | **Tauri 2** | **Electron** | **Compose Multiplatform (JVM)** | **Flutter desktop** | **Browser / PWA** | **Native ×3** |
|---|---|---|---|---|---|---|
| UI tech | web UI in OS webview | web UI in bundled Chromium | Kotlin Compose (same API family as the Android client) | Dart/Flutter | web | Swift / C# / GTK |
| Core language | Rust | Node.js | Kotlin/JVM | Dart (+ native plugins) | JS | per-OS |
| Windows / macOS / Linux | ✓ / ✓ / ✓ (WebKitGTK on Linux) | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | browser-dependent | separate apps |
| Tray | official tray API | mature | built-in `Tray` composable | community plugin; Linux clicks not reported | **no persistent tray** | ✓ |
| Global hotkey | official plugin (Linux/Win/mac) | built-in (Wayland gaps) | no built-in; native lib needed | community plugin | **not available** | ✓ |
| Secure credential storage | via OS-keychain plugin / Rust keyring crates | `safeStorage` (**plaintext fallback on Linux without a secret store**) | native interop needed | community plugins | WebCrypto non-extractable keys (browser-bound) | ✓ |
| Updater | official, **signature mandatory** | mature ecosystem | DIY | DIY / community | n/a (web deploy) | per-OS |
| Footprint | smallest (uses OS webview) | largest (ships Chromium) | JVM runtime in bundle | medium | none to install | smallest |
| Code sharing with Android client | protocol/schemas only | protocol/schemas only | **UI + client logic** (Kotlin) | none with the Kotlin Android app | none | none |
| Attack surface | Rust core + webview; capability-scoped IPC | Node + Chromium in one app; needs careful hardening | JVM + native interop | Dart + plugins | browser sandbox (small, but weak background) | smallest per OS |
| Solo-developer cost | medium | low–medium | medium | medium | lowest | highest |

### 4.2 Research notes behind the table

- **Tauri 2** `[RESEARCH]`:
  - stable since October 2024;
  - apps run a web UI inside the OS webview and talk to a Rust core;
  - global shortcuts and notifications are official plugins, with global shortcuts listed for Linux, Windows and macOS;
  - the updater **requires** a signature, and that cannot be disabled;
  - the updater has "dangerous" options to accept invalid TLS certificates for internal servers. JARVIS production builds must never enable them.
- **Footprint** `[RESEARCH]`: third-party benchmarks consistently show Tauri bundles and idle memory well below Electron, but the numbers vary by author. A Tauri maintainer-side discussion notes that on Windows, Tauri runs on WebView2 (Chromium-based) and memory is similar to Electron there. The footprint advantage is therefore real mainly on macOS and Linux and in download size.
- **Linux webview** `[RESEARCH]`: Tauri renders through WebKitGTK on Linux, which comparisons report as having performance issues on some distributions.
- **Electron** `[RESEARCH]`:
  - `safeStorage` uses DPAPI on Windows and Keychain on macOS;
  - on Linux it uses kwallet or gnome-libsecret, but some setups have no secret store;
  - there it encrypts with a hardcoded plaintext password and reports `basic_text`.
  - Any Electron build for JARVIS would have to detect `basic_text` and refuse to persist a credential.
- **Compose Multiplatform** `[RESEARCH]`: targets the JVM on macOS, Windows and Linux, provides tray and notification components, and ships native distributions (DMG/MSI/EXE) through its Gradle plugin.
- **Flutter desktop** `[RESEARCH]`:
  - tray and hotkey support come from community plugins;
  - the main tray plugin just made breaking changes;
  - on Linux it does not report tray icon clicks at all.
- **PWA** `[RESEARCH]`: practitioners report no persistent tray icons and unreliable background operation. Background notifications work only while a browser or PWA window stays open.

### 4.3 Reasoning

- **Browser/PWA fails the core requirement.** An always-available assistant with a hotkey, tray and dependable background presence is exactly what PWAs do poorly. `[PROPOSED]` keep a browser surface as a **future low-capability endpoint** (status, history, confirmations with a browser-bound key), not the desktop client.
- **Native ×3 is unaffordable** for one developer and buys integration release 1 does not need.
- **Flutter** shares nothing with the Kotlin Android client, and its desktop integration rests on community plugins with Linux gaps.
- **Electron** is the safe, mature choice. Its costs are a large always-running footprint and a Linux credential fallback that JARVIS would have to guard against.
- **Tauri vs Compose Multiplatform** is the real choice:

| Criterion | Tauri 2 | Compose Multiplatform |
|---|---|---|
| Always-on footprint | better (macOS/Linux); similar on Windows | JVM baseline |
| Credential isolation | the key lives in the Rust core; the webview never sees it | good, same process as the UI |
| Global hotkey | official plugin | needs a native library |
| Future character presentation | web animation runtimes available | Compose canvas / native runtimes |
| Reuse with Android | contracts only (generated from `shared/`) | UI components, view models, channel client |
| Your skills | web UI design fits a designer's workflow | same language as the Android client |

`[PROPOSED]` **Tauri 2** as the candidate. The desktop client's main job is to sit quietly in the tray and keep a credential away from its own UI, which is what Tauri's split does best. What JARVIS must share across clients is the **protocol**, and that is language-neutral: types generated from `shared/schemas` and the mapping table (`23` §5.1).

`[OPEN — OWNER]` OD-EP-1: choose Tauri 2, or Compose Multiplatform for maximal Android reuse.

---

## 5. Recommended candidate architecture `[PROPOSED]`

```
┌──────────────────────── Desktop endpoint (Tauri 2) ────────────────────────┐
│                                                                            │
│  Web UI (webview)                    Rust endpoint core                    │
│  ─────────────────                   ──────────────────                    │
│  floating panel, task list,   IPC →  login handoff (system browser)        │
│  confirmation screens,       (only   device key: generate / sign proofs    │
│  settings, PresentationState allow-   token refresh, step-up               │
│  renderer (character later)  listed  WebSocket channel to gateway          │
│                              cmds)   notification + tray + hotkey          │
│  NO token, NO key,                   adapter host (empty in release 1)     │
│  NO direct network to server         device-side guard (grants, table ver.)│
│                                      OS credential store / hardware key    │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │ HTTPS / WSS only
                                       ▼
                         JARVIS gateway (unchanged core)
         identity → principal → task → 04 authorization → tiers/confirm
                → endpoint adapter dispatch to exactly device_id
```

Rules:
- **The webview holds no credential and opens no connection to the server.** All server traffic goes through the Rust core. A compromised page (e.g. via rendered task output) cannot steal the token.
- **IPC surface is an allowlist** of named commands ("submit task", "confirm action", "cancel task", "open settings"). There is no generic "fetch" command and no "run" command.
- **No local listening port**, except a one-shot loopback listener during login (§9.2). No other local program can drive JARVIS through the client. A single-instance lock uses the OS's per-user mechanism.
- **Task output is rendered as untrusted content**: markdown sanitized, no remote resources, no script. The same doctrine applies as PRD §24.

---

## 6. Windows assessment

| Topic | Finding / proposal |
|---|---|
| Webview | WebView2 (Chromium-based); present on current Windows. Memory advantage over Electron is small here (§4.2) |
| Tray, hotkey, notifications | supported by Tauri plugins; no special permission model for global hotkeys |
| Credential storage | `[PROPOSED]` software key protected by DPAPI / Credential Manager (per-user). Hardware-bound option: a TPM-backed key through the platform crypto provider — P-256, not Ed25519 (§9.3) |
| Microphone | Windows privacy settings gate microphone access per app; the client must handle "denied" gracefully |
| Screen/context (later) | UI Automation for structured semantics; screenshot APIs; OCR via OS or bundled model |
| ARM64 | Windows on ARM exists and Tauri targets it; build and test an ARM64 artifact if the owner wants it (not required for release 1) |
| Signing | §13: OV certificate route, since the cheap Microsoft service is region-limited |

---

## 7. macOS assessment

| Topic | Finding / proposal |
|---|---|
| Webview | WKWebView |
| Distribution | `[RESEARCH]` apps outside the Mac App Store must be signed with a Developer ID certificate and notarized, or Gatekeeper blocks them; the Hardened Runtime is required for notarization |
| Permissions (TCC) | `[RESEARCH]` Accessibility and Screen Recording are separate, user-granted permissions. Utility apps commonly need Accessibility for global input features and Screen Recording for capture; macOS usually requires a relaunch after granting Screen Recording. Release 1 needs neither if the hotkey plugin works without Accessibility — `[verify at implementation]` |
| Credential storage | Keychain for a software key; Secure Enclave for a hardware-bound P-256 key (§9.3) |
| Architecture | ship a universal binary (Apple Silicon + Intel) |
| Menu bar | tray API maps to the menu bar; a menu-bar-only app is the natural shape |

---

## 8. Linux assessment

This is the section where the matrix should *not* be symmetrical.

| Topic | Finding |
|---|---|
| Global shortcuts on Wayland | `[RESEARCH]` The GlobalShortcuts portal is the user-approved path on supporting desktops. GNOME added its backend in GNOME 48, after Ubuntu 24.04's GNOME 46; KDE and Hyprland have implementations. Availability must be detected from the actual D-Bus interface, not guessed from the session type |
| Newer GNOME | `[RESEARCH]` On GNOME 50 with xdg-desktop-portal 1.20+, non-sandboxed apps must register a host-app identity first. At least one Electron app's global shortcut silently fails there until the framework adopts it — a concrete example of framework lag |
| Secret storage | `[RESEARCH]` not every setup has a secret store; Electron's fallback is plaintext. The same risk exists for any framework using Secret Service |
| Tray | StatusNotifierItem / AppIndicator; depends on the desktop's panel support |
| Webview | WebKitGTK (Tauri); performance varies by distribution |
| Packaging | Tauri updater supports Deb, Rpm and AppImage |

`[PROPOSED]` Linux policy:
- **Tier 2, Ubuntu LTS first** (`.deb`), X11 and Wayland sessions, GNOME default desktop. Others are best-effort.
- **Hotkey:** use the portal where the interface is present. Everywhere else, ship a `jarvis --toggle` command that talks to the running instance, and document binding it as a desktop custom shortcut. That fallback works on every Linux desktop.
- **Credential:** if no Secret Service backend is available, **do not persist the device credential**. The client runs session-only and asks the user to log in each launch, with a clear message. Never write a plaintext credential.
- **Perception on Linux** (later) goes through the ScreenCast/Screenshot portals, with per-use consent on Wayland; AT-SPI for structured semantics.
- **Maintenance budget:** one reference machine/VM (Ubuntu LTS, GNOME, Wayland) in the test matrix. Other desktops are community-reported.

`[OPEN — OWNER]` OD-EP-4: confirm Linux as tier 2 with Ubuntu LTS first.

---

## 9. Authentication / device registration model

### 9.1 The chain stays the same `[LOCKED]`

```
Google OIDC (server is the OIDC client) → internal user_id (subject-keyed)
  → device registration (endpoint generates its own key pair; server stores the public key)
  → device credential (proofs signed by the endpoint's private key)
  → short-lived access token, refreshed with a proof; step-up for sensitive actions
```

Desktop gets **its own device credential**, exactly like each Android device. A user with two phones and a laptop has three devices, three credentials, three independent revocations. `[LOCKED]` (register §1) a user's devices share that user's authorized state; nothing crosses users.

### 9.2 Getting the login result back to the app `[PROPOSED]`

`[RESEARCH]` RFC 8252 requires native apps to perform OAuth authorization in an **external user agent** (the system browser) and requires PKCE for public native clients.

JARVIS's server is the OIDC client with Google, so the pattern applies to the *JARVIS bootstrap handoff*:

1. The endpoint generates a random `verifier` and sends `challenge = S256(verifier)` with a `return_to` when it opens the system browser at `/auth/oidc/start`.
2. Google login completes at the server. The callback **redirects** to `return_to` with a one-time `bootstrap_code` instead of returning JSON.
3. The endpoint redeems `bootstrap_code + verifier`, then registers its device key.

`return_to` is restricted by server config to:
- **loopback** `http://127.0.0.1:{any port}/cb` — desktop, RFC 8252 §7.3;
- the registered **Android App Link** host — `23` §3;
- `[FUTURE]` a device-code flow for input-less endpoints (§17).

Why each piece:
- Binding the code to the endpoint's challenge means an app that intercepts the loopback redirect cannot redeem it. This is the known loopback-interception risk RFC 8252 discusses.
- The code is single-use and short-lived.

This **one server change serves Android and desktop**. It replaces the JSON-callback gap flagged in `23` §3, so it should be built once, before either client.

### 9.3 Where the private key lives

| Option | Platforms | Extractable by same-user malware? | Needs server change? |
|---|---|---|---|
| **A. Ed25519 key in the OS credential store** (Keychain / DPAPI-Credential Manager / Secret Service) | all three | yes, with effort (same-user) | no |
| **B. Hardware-bound P-256 key** (Secure Enclave / TPM) | macOS, Windows | no (non-exportable) | **yes**: accept `ecdsa_p256` device credentials beside Ed25519 |

`[PROPOSED]` add algorithm agility to device credentials (`ed25519` | `ecdsa_p256`, recorded per device). Use B where available and A as the fallback. `[OPEN — OWNER]` OD-EP-2. It touches `03` and `server/auth/device.py`, though not authorization.

Either way, `[LOCKED]` a stolen device credential remains the documented SEC-J residual: revocable, short-lived access tokens, and step-up for tier 4.

### 9.4 Step-up on desktop

`[PROPOSED]` step-up = OS user-presence prompt (Touch ID / Windows Hello) gating a fresh re-attestation signature, where available; otherwise re-login through the browser. **Never** a typed phrase stored in the app, and never voice (`27` §3).

---

## 10. Capability / security model

### 10.1 Endpoint compromise assumptions

| Assumption | Consequence |
|---|---|
| Same-user malware on the laptop can read many app secrets, inject input, and read the screen | the desktop credential is treated like any stolen device (SEC-J): revocation, short tokens, step-up; a compromised desktop can never exceed its user's own grants |
| The client binary could be modified | the server never trusts client-asserted identity, grants, or tiers; two-layer enforcement means a modified client can only *refuse more*, never permit more |
| Webview content could be hostile (rendered task output, perceived screen text) | no credential and no network in the webview; output sanitized; screen text is data (PRD §24) |
| Update channel could be attacked | signed updates only, public key pinned in the build (§13) |

### 10.2 Desktop capability map

| Class | Examples | Proposed placement |
|---|---|---|
| **Core endpoint (no capability)** | UI, text input, task status, notifications of task events, confirmation screens | release 1 |
| **Input methods (no capability)** | voice capture as input (`27`), user-initiated paste or attach file | release 2 |
| **Low-risk execution** | read active app/window title, battery/power (`device.read` on a desktop adapter); `file.read` in **folders the user granted** | release 3 |
| **Medium** | `file.write` in granted folders (tiers per matrix: create/write `low_write`, delete `consequential`, bulk delete `high_irreversible`); `app.launch` (`low_write`) | release 4 |
| **Perception** | Accessibility tree (UIA / AX / AT-SPI) → OCR → screenshot, the same ladder as `23` §6 with the same redaction rules | release 4+ |
| **High-risk** | keyboard/mouse injection, clipboard **read** by the agent, browser automation writes | `[FUTURE]`; at least `consequential`, and the sensitive-app classification (matrix §5.1) applies |
| **Not planned** | shell / process interaction on the desktop, system controls (network, security settings, shutdown) | no adapter. There is no cross-platform kernel confinement equivalent to the server's Landlock mode, so `system.restricted` stays server-only |

`[PROPOSED]` desktop file access uses a **per-folder grant grid** (the per-app grid of PRD §13, applied to folders), and a desktop filesystem adapter that re-implements `09`'s containment on the client OS. That means rejecting traversal, symlinks, Windows junctions and macOS aliases that escape a granted folder. It is enforced device-side as the second layer; the server remains the first.

### 10.3 Platform vocabulary

`[PROPOSED]` new `ExecutionPlatform` values `desktop_windows`, `desktop_macos`, `desktop_linux`, never `linux` (server host, §2.2 item 3). A capability with no adapter on a platform is rejected as today (`unsupported_platform`).

### 10.4 Cross-device execution — deliberately not proposed

"From my laptop, open WhatsApp on my phone" would need an operation authorized on one device's session to execute on another device. Today OD-DEV-1 forbids that. Allowing it would change the authorization contract (target-device grants, delivery rules, confirmation on which screen) and is therefore outside this proposal.

`[OPEN — OWNER]` OD-EP-6: keep "each endpoint executes only its own operations" for now (recommended), or commission a separate authorization proposal.

### 10.5 Cross-device confirmation

Because tokens bind to the session, a phone-started consequential action cannot be approved from the laptop. Allowing it is narrower than cross-device execution: the action still runs where it was authorized, only the approval moves. It would require:
- a token bound to principal (not session);
- delivery of the confirmation prompt to the user's other active endpoints;
- step-up on the approving device for tier 4.

`[OPEN — OWNER]` OD-EP-5; recommended **defer**. Release 1 confirms on the originating endpoint.

---

## 11. Voice / audio integration

| Piece | Proposal |
|---|---|
| Capture | `[PROPOSED]` native capture in the Rust core, not webview `getUserMedia`, for consistent permissions and background use. macOS needs the microphone usage description; Windows privacy settings gate microphone access |
| Speech-to-text | desktops have no uniform on-device OS speech API across all three platforms. Options: **(a)** server STT provider through `/voice/transcribe` (`27` allows; audio transient, not retained); **(b)** a bundled local model in the client (private, heavier download and CPU). `[OPEN — OWNER]` OD-EP-3; recommended (a) for release 2, (b) as an optional download later |
| Text-to-speech | OS voices (each platform has a built-in TTS service); server TTS optional |
| Rules | `[LOCKED]` (`27`) speaker identity is never authentication; voice cannot confirm or satisfy step-up; raw audio not retained by default |

---

## 12. Notifications / global hotkeys / system integration

- **Notifications:** native OS notifications carrying only what the user may see (their own task events). Actionable notifications open the relevant screen; they never approve an action directly. Approval happens on the confirmation screen.
- **Hotkey:** default `Ctrl+Space` / `⌥Space`-style, user-remappable, conflict-detected. Linux fallback as §8.
- **Floating panel:** borderless, always-on-top when summoned, dismissed on Esc or focus loss. `[PROPOSED]` the panel consumes the same `PresentationState` stream defined in `23` §7, so a future character renders identically on phone and desktop.
- **Start at login:** off by default; user toggle.
- **Sleep/resume:** reconnect the channel on resume or network change. While disconnected, server operations for this device fail `device_unavailable` (`23` §4, no queuing).

---

## 13. Update / distribution / signing strategy

| Platform | Proposal | Research basis |
|---|---|---|
| All | Tauri updater with a signing key kept offline or in CI secrets; public key compiled into the app; HTTPS only; never enable the invalid-certificate options | the updater signature cannot be disabled |
| macOS | Apple Developer Program, Developer ID signing, Hardened Runtime, notarization, universal binary | required for Gatekeeper outside the App Store |
| Windows | Authenticode-sign every release with **one consistent certificate** so SmartScreen reputation accumulates | Microsoft's managed signing service is limited to organizations in the USA, Canada, EU and UK; OV certificates (~$150–300/yr) are the route elsewhere; EV no longer gives instant SmartScreen reputation; since 2023 code-signing keys must live on hardware (HSM/token), so cloud-HSM signing in CI is the practical setup |
| Linux | `.deb` for Ubuntu LTS, AppImage as a portable option; signed update artifacts through the Tauri updater | updater supports Deb/Rpm/AppImage |

Supply chain `[PROPOSED]`:
- lockfiles committed;
- `cargo audit` + `npm audit` in CI;
- an SBOM per release;
- builds only in CI from tagged commits;
- signing keys never on developer laptops;
- lint and type checks for the Rust and TypeScript code from the first commit (the repository has none today).

`[OPEN — OWNER]` OD-EP-7: signing budget and identity. Apple ~$99/yr plus a Windows OV certificate; from India the Microsoft managed service is not available.

---

## 14. Local UTM testing topology

```
Option 1 (recommended — same path as production)
  desktop / Android ──HTTPS──► public hostname (named Cloudflare tunnel) ──► UTM Ubuntu VM gateway

Option 2 (offline development)
  desktop ──HTTPS──► VM on a bridged/host-only network, TLS cert from a local dev CA
  Android ──► same, trusting the dev CA only in debug builds (network security config debug-overrides)
```

- Option 1 also satisfies Google's redirect-URI and Android App Link needs (`NEXT_BUILD_INDEX.md` §6 item 3), so one setup serves both clients.
- `[PROPOSED]` the client's server base URL is build-profile config. Dev builds may trust a local dev CA; **release builds trust only the public Web PKI** and have no "ignore certificate" switch.
- `[PROPOSED]` no certificate pinning. Tunnel and CA certificates rotate, and pinning adds outage risk without closing a threat the rest of this design leaves open.

---

## 15. Cloud deployment compatibility

- The client knows only a base URL. Identity is server-derived, so the same binary works against a laptop, a VPS or a multi-user deployment.
- The client contains **no deployment secret**: no Google client secret (the server is the OIDC client), no API keys, no tunnel credentials (`[LOCKED]` SECRET-004, REPO-T2).
- `[PROPOSED]` a self-hoster can point the client at their own server in settings; switching servers wipes the local device credential and requires a new login and registration.

---

## 16. Future endpoint abstraction

`[PROPOSED]` a generic **Endpoint** model. Most of it already exists as `Device` plus sessions.

```
Endpoint
├── identity      Device row: device_id, user_id, endpoint_class, platform, credential_alg, public key
├── session       short-lived tokens bound to device (exists)
├── transport     WSS channel (23 §4) | parent-relayed (accessory) | device-code enrolled (constrained)
├── input         text | voice | touch | sensor telemetry            (untrusted data into the server)
├── output        display | audio | haptic | HUD overlay | notification
├── perception    screen | camera | environment sensors             (untrusted data; ladder + redaction)
├── execution     adapters for enumerated primitives (optional; many endpoints have none)
└── profile       what the endpoint says it can do — used for ROUTING ONLY, never for authorization
```

Rules:
- `[PROPOSED]` `endpoint_class ∈ {android, desktop, browser, audio_accessory, hud, sensor, custom}` is a new field on `Device`.
- `[LOCKED-in-spirit]` the **profile is an advertisement, not a grant**. The server uses it to avoid sending an operation an endpoint cannot perform. Authority still comes only from the user's grants for that device, checked by `04`.
- Output-only and input-only endpoints need **no execution adapters** and therefore sit entirely outside the capability registry. They are UI surfaces, like the desktop in release 1.

---

## 17. IoT / wearable compatibility analysis `[FUTURE]`

| Device class | Role | Enrollment | Transport | What stays server-side |
|---|---|---|---|---|
| **Wireless earbuds** | input (mic) + output (audio) | **none of its own** — an accessory of a parent endpoint (phone/desktop) | Bluetooth to the parent; the parent relays | everything; the earbud is an I/O channel of its parent's authenticated session |
| **Smart glasses / AR HUD** | output (HUD) + input (voice, gestures) + perception (camera) | direct enrollment if it has its own network stack and a companion app; else an accessory of the phone | WSS or parent-relayed | authorization, confirmation (confirm on the phone screen or with the glasses' own authenticated UI, never by voice), camera data handling |
| **IoT sensor** | input only (telemetry) | pairing: an authenticated phone issues a one-time enrollment code; or device-code flow (RFC 8628-style) | MQTT/HTTPS to a gateway ingest endpoint | all interpretation; sensor data is untrusted input, never a trigger for consequential action without a user-instructed task |
| **Custom hardware / actuator** | execution (e.g. a relay, a lock) | same as IoT, with its own credential | WSS | the capability, the tier (physical actuation is at least `consequential`; locks and doors are `high_irreversible`), confirmation |
| **Browser** | UI + confirmations | OIDC via the page; browser-bound non-extractable key | HTTPS/WSS | same as desktop release 1 |

Principles:
1. **Accessories do not become devices.** No credential, no grants; they extend a parent endpoint's I/O. This avoids giving a pair of earbuds an identity that could be stolen.
2. **Constrained devices never run OIDC.** They enroll through a user's authenticated endpoint and receive a narrow, revocable credential with a narrow capability profile.
3. **Nothing an endpoint senses is authority.** Presence, voice or a sensor reading can raise context or suggest a task, never authorize one (`[LOCKED]` INV-14 generalizes this).
4. **Physical actuation is never automatic** in the tier table.

---

## 18. Risks and trade-offs

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Tauri webview inconsistency (WebKitGTK on Linux, WKWebView vs WebView2) | medium | UI bugs per platform | tier Linux; test the UI on all three; keep the UI simple until release 3 |
| Credential extraction by same-user malware | medium | account access until revoked | hardware keys where possible (OD-EP-2), short tokens, revocation, step-up |
| Linux credential-store absence | medium | plaintext exposure | refuse to persist (§8) |
| Wayland hotkey gaps | high on GNOME < 48 | UX friction | portal where present, `--toggle` fallback |
| SmartScreen warnings on early Windows releases | high | install friction | consistent signing identity; reputation builds over releases |
| Two client codebases (Kotlin Android + Tauri desktop) drift | medium | inconsistent behaviour | generate protocol types from `shared/`; shared conformance vectors (`23` §9) |
| Scope creep into desktop execution | high | new attack surface | release 1 has zero adapters; each capability added separately with its own tests |
| Always-on footprint on Windows similar to Electron | medium | "why not Electron" doubts | accept; the security split (Rust core) is the stronger reason |

---

## 19. Required changes to existing docs/contracts (follow-ups, not made here)

| Doc / code | Change | Needed for |
|---|---|---|
| `02`, `server/gateway/routers/auth.py` | `/auth/oidc/start` accepts `return_to` + `challenge`; callback redirects with a one-time `bootstrap_code`; new redeem endpoint takes `code + verifier`; config allowlist of return patterns | both clients (also closes `23` §3) |
| `03`, `server/auth/device.py`, `01` | `credential_alg` per device (`ed25519` \| `ecdsa_p256`) | OD-EP-2 |
| `01` §1.2, `Device` | `endpoint_class`, `platform`; `ExecutionPlatform` gains `desktop_*` values | desktop routing |
| `23` §4 | generalize "device channel" to "endpoint channel"; FCM wake stays Android-only | desktop transport |
| `CAPABILITY_MATRIX.md` | desktop platform columns; per-folder `file.*` scope on desktop | release 3+ |
| `15` | `auth.return_to_allowlist`; client distribution/update config | login handoff |
| `16`, `pyproject.toml` | `desktop/` peer package sharing only `shared/`; boundary lint | repo layout |
| `17` | endpoint tests (§20 sequence) | acceptance |
| `NEXT_BUILD_INDEX.md` | add this assessment and its decisions | tracking |

---

## 20. Open owner decisions

| ID | Decision | Recommendation |
|---|---|---|
| **OD-EP-1** | Desktop framework | Tauri 2 (alt: Compose Multiplatform) |
| **OD-EP-2** | Add P-256 device credentials for hardware-bound desktop keys | yes |
| **OD-EP-3** | Desktop STT placement | server STT for release 2; optional local model later |
| **OD-EP-4** | Linux tier | tier 2, Ubuntu LTS `.deb` first |
| **OD-EP-5** | Cross-device confirmation | defer; confirm on the originating endpoint |
| **OD-EP-6** | Cross-device execution | not now; separate authorization proposal if wanted |
| **OD-EP-7** | Signing budget/identity (Apple + Windows OV) | buy both before the first public release; unsigned builds for personal testing only |
| OD-EP-8 | Windows ARM64 artifact in release 1 | no, unless you use a Windows ARM device |
| OD-EP-9 | Ratify the generic Endpoint model (§16) and `endpoint_class` | yes |

---

## 21. Implementation sequence after approval

```
0. Owner decisions OD-EP-1..7.
1. Server (shared with Android, do once):
   login handoff with return_to + challenge + one-time code; credential_alg (if OD-EP-2);
   endpoint_class/platform fields; endpoint channel generalized from 23 §4.
2. Desktop release 1 (Windows + macOS): tray, hotkey, floating panel, text tasks, live status,
   notifications, confirmations, step-up; zero adapters.
3. Signing + updater pipeline in CI (Developer ID + notarization; Windows OV via cloud HSM).
4. Linux tier-2 build (.deb), portal hotkey + --toggle fallback, credential-store check.
5. Voice (release 2): native capture, server STT, OS TTS.
6. Release 3: desktop device.read + file.read in granted folders, with device-side guard and
   09-equivalent containment tests; BR-T2 rows for the desktop dimension.
7. Later, one at a time: file.write, app.launch, perception ladder. Input injection and shell stay out.
```

Each step keeps the real-data gate unchanged (`17` §5 and `NEXT_BUILD_INDEX.md` §3).

---

## 22. Sources

| Source | Supports |
|---|---|
| Tauri 2.0 stable release post — v2.tauri.app/blog/tauri-20 | stable date, OS-webview architecture, plugin model, global-shortcut and notification plugins |
| tauri-plugin-global-shortcut — crates.io | Linux/Windows/macOS support table |
| Tauri updater docs — v2.tauri.app/plugin/updater | mandatory update signatures |
| tauri-apps/plugins-workspace updater-js v2.10.0 release notes | invalid-cert options; Deb/Rpm/AppImage and NSIS/MSI support |
| Electron `safeStorage` docs — electronjs.org | DPAPI/Keychain/libsecret/kwallet backends; `basic_text` plaintext fallback |
| tauri-apps/tauri issue #5889 | WebView2 memory similar to Electron on Windows |
| DoltHub "Electron vs Tauri" (2025); PkgPulse; buildmvpfast comparisons | footprint differences; WebKitGTK performance caveat; rendering-consistency trade-off |
| JetBrains Compose Multiplatform desktop docs; kotlinlang native distributions | desktop tray/notifications; native packaging |
| leanflutter tray_manager v0.7.0 release; hotkey_manager | Flutter desktop plugin status; Linux tray click limitation |
| Ken Vermette, "PWAs Without the Browser?" (2026); 3CX community thread | PWA lack of persistent tray; unreliable background |
| yuxino/kiri issue #47 | GNOME 48 GlobalShortcuts backend vs Ubuntu 24.04 GNOME 46; detect via D-Bus |
| aaddrick/claude-desktop-debian learnings doc | GNOME 50 / portal ≥1.20 host-app registration gap |
| ArchWiki XDG Desktop Portal | portal backend support matrix |
| RFC 8252 (rfc-editor.org) | external user agent requirement; PKCE for native apps; loopback redirects and their interception risk |
| PinTop and similar macOS utility READMEs; electron.build notarization docs | Developer ID + notarization requirement; Accessibility and Screen Recording TCC permissions; Hardened Runtime |
| Microsoft Learn "Code signing options for Windows app developers" | managed signing service regions and price; OV/EV costs; EV no longer grants instant SmartScreen reputation |
| SCIRun issue #2742; DigiCert and ToDesktop notices | SmartScreen reputation is earned; hardware key storage requirement since 2023 |

Claims marked `[verify at implementation]` (hardware key algorithms in Secure Enclave/TPM; whether the Tauri hotkey plugin needs macOS Accessibility) come from platform knowledge, not a source fetched in this pass.

---

*End of 24. Proposal only — not canonical until ratified.*
