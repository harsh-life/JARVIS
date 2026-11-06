# The animated character on Android (Rigby2D rig)

How the Android client draws the skeletal JARVIS character instead of the seven static images,
and what to know before changing it. The character is still presentation only
(`docs/CHARACTER_PRESENTATION.md`): `CharacterController` decides the expression and its intensity;
this renders them (§5 for intensity).

## 1. What runs

```
CharacterController ─ expression ─▶ Character() ─▶ AnimatedCharacter ─▶ RigCharacterPlayer ─▶ RigRuntime
   (unchanged)                       (ui/)           (Compose, lifecycle,    (the contract:         NativeRigRuntime
                                        │             frames, 10-min timer)   which animation)       │ JNI
                                        └─ static images (fallback) ◀── no rig                      libjarvis_rig.so
                                                                                                     (Rigby2D runtime)
```

| Piece | Where | Does |
|---|---|---|
| Rig files | `assets/character/rig/` | `JarvisCharacter.skel`, `.atlas`, `.png`, `.contract.json`: the rig project's exports, byte for byte (`SHA256SUMS`, pinned by `RigContractTest`) |
| Intensity profile | `assets/character/rig/JarvisCharacter.intensity.json` | JARVIS's own, beside the exports (also pinned in `SHA256SUMS`): per expression, a posture gain and an idle-motion gain for each intensity, and the bones each keeps canonical (§5) |
| Native library | `android/rig-native/` | `rigby-runtime/`: the Rigby2D Rust runtime, vendored unmodified (`UPSTREAM.md`). `jarvis-rig/`: the JNI bridge (`rig.rs` core, `jni.rs` entry points) |
| Contract | `rig/RigContract.kt` | Parses the control contract; refuses a malformed or incomplete one whole |
| Player | `rig/CharacterAnimationPlayer.kt`, `rig/RigCharacterPlayer.kt` | Expression → animation, per the contract (below); intensity → the profile's two gains |
| Intensity | `rig/RigIntensityProfile.kt`, `rig/RigShaping.kt`, `jarvis-rig/src/shape.rs` | Parses and validates the profile; applies it to the native rig at load; the native posture and idle-motion layers (§5) |
| Runtime port | `rig/RigRuntime.kt`, `rig/NativeRig.kt` | Tracks, queue, time, geometry; the native rig's handle and lifetime |
| Drawing | `rig/RigPainter.kt`, `rig/RigCharacterView.kt` | Fits the frame into the slot; one `drawVertices` per frame with a `BitmapShader` over the atlas page |
| Long idle | `rig/LongIdleSchedule.kt` | The application-owned ten-minute timer (the runtime has none) |

All of it sits under `presentation/character/` and imports nothing outside it
(`CharacterBoundaryTest`). Nothing outside that package sees the runtime.

## 2. Behaviour

- **Change of state** A → B: `trans_A_to_B` if the contract has it (0.15 s mix), else `to_B` (0.3 s),
  then `idle_B` queued (0.2 s). During a transition or long idle the change mixes in 0.15 s and drops
  the queue, so an old idle never plays after a new state.
- **Same state again:** nothing restarts.
- **Long idle:** after ten minutes of one state on screen, `long_idle_<state>` once, then its idle.
  It is refused during a transition. The count stops in the background and restarts on a state change.
- **Entrance:** `enter` on track 1 when the character first appears; the track is cleared when it
  completes. `exit` is wired in the player but nothing calls it yet (the app never hides the
  character with an animation).
- **Lifecycle:** frames run only while the screen is resumed. Paused, time stands still and a resume
  does not jump; leaving the composition releases the native rig and the page bitmap.
- **Reduced motion** (`reduced_motion: always`, or the system's animations off): the state's idle,
  still, redrawn only when the state changes. No entrance. Intensity is ignored (the profile is not
  even applied).
- **Intensity:** a new intensity for the shown expression only retargets the gains (ramping); no
  animation restarts. The same (expression, intensity) again is a no-op (§5).
- **Fallback:** if the native library is absent for this device, or a file is missing or invalid,
  the seven static images show (they also show while the rig loads; the rig's first frame is the
  same as `default.png`).

## 3. Native side

- **ABIs:** arm64-v8a, armeabi-v7a, x86_64 (NDK `27.2.12479018`, API 24). Other ABIs have no
  library, so they fall back.
- **Ownership:** `nativeCreate` returns a boxed rig as a handle. `NativeRigRuntime.close()` frees it
  exactly once; any later call throws. Only primitive arrays cross JNI: no strings, objects, global
  references or callbacks are kept. Array lengths are checked before every write, and panics are
  caught at every entry point.
- **No per-frame allocation:** geometry buffers are sized at load for the worst case (every slot's
  largest attachment) on both sides; events go into a reused buffer.
- **Refused rigs:** several atlas pages, clipping, linked meshes, blend modes other than normal and
  two-color tint are not drawn by this player, so such a rig fails to load (→ fallback) rather than
  drawing wrongly. Vertex colors are not used (this rig has no tint keys).
- **API < 29:** a hardware-accelerated canvas skips `drawVertices`, so the frame is drawn in software
  into a reused bitmap the size of the slot.

## 4. Building and testing

The app build runs `cargo` (`buildRigNative_*`, and `buildRigNativeHost` for the unit tests). You
need a Rust toolchain with the three targets and the NDK:

```bash
rustup target add aarch64-linux-android armv7-linux-androideabi x86_64-linux-android
sdkmanager "ndk;27.2.12479018"
./gradlew :app:assembleDebug                      # or -Pjarvis.rig.native=false: no library, static images
cd rig-native && cargo test --release --locked    # the core against the bundled rig
```

`NativeRigRuntimeTest` and `NativeRigIntensityTest` run the real library (host build) from plain
JUnit; a native library binds to one class loader, so Robolectric tests never load it and always
exercise the fallback. When the build is native (the default), a missing host library fails those
tests rather than skipping them.

To update the character: re-export from the rig project, copy the four files into
`assets/character/rig/`, regenerate `SHA256SUMS`, and run the tests above. The contract and the
skeleton must agree, or the player refuses the rig.

## 5. Presentation intensity

`CharacterController` commits a `PresentedCharacter(expression, intensity)`; intensity (1–10) is
how strongly the chosen expression is presented. It is derived deterministically from the selected
state's **suitability** score and threshold (`docs/CHARACTER_PRESENTATION.md` §3a) — it is not the
score, not the confidence, and the player never sees either, nor anything that was said. The
player receives an expression and an integer; nothing here selects an expression or infers
anything, and nothing outside presentation reads any of it.

```
Selection(expression, score) ─▶ CharacterController ─▶ PresentedCharacter(expression, intensity)
   (StateSelector, unchanged)      + IntensityPolicy        │
                                                            ▼
                     RigCharacterPlayer ── profile ──▶ (posture gain, idle-motion gain)
                                                            │ JNI, on change only
                                                            ▼
              canonical pose (AnimationState, unchanged) + posture layer + idle-motion layer + guard
                                                            ▼
                                                  world transforms ─▶ RigPainter (unchanged)
```

### 5.1 One intensity, two internal dimensions

- **Posture emphasis** adds `(posture gain − 1) × (held pose − reference held pose)` per bone. An
  idle's *held pose* is its time average over one loop, sampled once at load with the runtime's own
  sampler; the *reference* is Default's held pose, computed the same way (for this rig it is the
  setup pose to within 0.02, but that is measured, not assumed).
- **Idle motion** adds `(motion gain − 1) × (idle pose at its time − held pose)`.

Both layers **belong to the regular idles**: each idle's layers are weighted by that idle's share
of track 0 (its crossfade weight). They are independent internally; the profile gives each its own
curve. Every gain ramps (smoothstep: posture 0.8 s, motion 0.5 s). With both gains exactly 1 —
intensities 5 and 6 — the native side touches nothing and the output is bit-identical to the
canonical animation.

### 5.2 What stays canonical

- The **animation assets**: skeleton, atlas, page, contract and every clip are unchanged.
- **Entries, pair transitions, long idles, the entrance and the exit play exactly as authored.**
  Leaving an idle, its layers blend out with the authored crossfade (0.3 s into a long idle or an
  entry); arriving, an idle's posture starts from 1 and eases in over its 0.8 s ramp (the ramp only
  runs while that idle is on track 0), so it never lands as a step. A frame inside a crossfade is
  the authored blend of the layered idle and the canonical clip.
- **Faces, blinks and draw order**: the layers only move bones; attachments never change.
- **Reduced motion, the static fallback and the software-rendering fallback** are unchanged.

*Changed from the first version,* in which the posture layer applied whatever played (so that an
expression change retargeted it during the transition) and continued through long idles. Entries
and long idles reach the rig's range at their authored peaks, and a constant offset on top of a
peak can only be trimmed by the guard: the first version needed the guard routinely (up to 2.5 in
sleepy's long idle, 0.8 in surprised's entry; §5.4). The exit still ramps both layers back to 1 and
the entrance re-applies them.

### 5.3 The profile (`jarvis-character-intensity/1`)

```json
{
  "schema": "jarvis-character-intensity/1",
  "reference_state": "default",
  "ramp_seconds": {"posture": 0.8, "motion": 0.5},
  "guard": "authored_range",
  "states": {
    "sad": {
      "posture": {"curve": [0.6, 0.65, 0.78, 0.9, 1.0, 1.0, 1.12, 1.22, 1.32, 1.4],
                  "hold_canonical": ["upperarm_r", "upperarm_l", "forearm_r", "forearm_l",
                                     "leg_r", "leg_l", "face"]},
      "motion":  {"curve": [0.25, 0.35, 0.55, 0.75, 1.0, 1.0, 1.12, 1.22, 1.32, 1.4],
                  "hold_canonical": ["face"]}
    }
  }
}
```

Validated whole (`RigIntensityProfile.parse`); anything else rejects it:

- exactly these keys, this schema, `guard: authored_range`, all seven states, a known
  `reference_state`;
- each curve ten finite numbers, non-decreasing, **exactly 1 at intensities 5 and 6**; posture
  within 0.6–2.0, idle motion within 0.25–2.0;
- bone names unique and non-empty, and present in the rig (checked when applied);
- ramps 0.05–5 s.

A missing, invalid or refused profile never drops to the static images: the rig plays canonically
at every intensity.

| State | Posture at 10 | Idle motion at 10 | Posture keeps canonical | Motion keeps canonical |
|---|---:|---:|---|---|
| happy | 1.50 | 1.70 | face | face |
| satisfied | 1.35 | 1.40 | face | face |
| default | 1.00 (it is the reference) | 1.30 | face | face |
| sleepy | 1.50 | 1.40 | **head**, shoulders, face | face |
| surprised | 1.25 | 1.70 | shoulders, **legs**, face | face |
| angry | 1.30 | 1.35 | shoulders, hands, face | hands, face |
| sad | 1.40 | 1.40 | shoulders, forearms, **legs**, face | face |

Below neutral every state uses posture 0.60/0.65/0.78/0.90 and motion 0.25/0.35/0.55/0.75 for
intensities 1–4 (Default's posture is 1 throughout). Above neutral, intensities 7–10 reach 30, 55,
80 and 100 % of the way to the top. The gains are the feasibility investigation's provisional
values; the bones in bold were added by the remediation (sleepy's head: §5.5; the legs: §5.4).
Checked geometrically and in emulator captures — **not calibrated on a physical device**.

Surprised draws much of its force from its entry transition, which stays canonical; its intensity
shows in its held reaction and idle only. Sleepy's posture emphasis is now carried by its body,
chest, forearms and hands; its head tilt is the authored one (its idle motion still scales).

### 5.4 The authored-range guard and its interventions

Each adjusted channel is kept within the range the rig's own track-0 animations reach for it
(sampled at load), never tighter than that frame's canonical value — so it only ever limits what a
layer added, and never clamps the canonical animation. It records every intervention per bone and
channel, the largest one and where, and what the layers asked for against what reached the pose
(`Shape::stats`, `Shape::channel_stats`; `NativeRigRuntime.shapeInfo`). It is a boundary for the
unexpected, not part of the look: a calibrated profile must not depend on it.

Why valid profile values used to reach it: the range's edges are set by the most extreme authored
moments — mostly long idles and entries. A posture offset is a constant; added on top of a peak that
already sits at an edge, it exceeds the range by exactly the offset wherever the offset points
outward. Sleepy's long idle turns the head to −10.59°, the head's range minimum, so sleepy's −2.5°
at 10 was trimmed by 2.498; angry's long idle sets the legs' edge on the side angry's reduced
posture (gain 0.6) moves toward, so even intensity 1 was trimmed by 0.999. The idles' own sway
also mattered where an idle sits near a narrow range: sad's right leg is held at −178.0° with an
authored top of −176.74°, and posture (+0.80) with sway scaled ×1.4 (±0.84) crossed it; surprised's
legs likewise (posture −0.62, sway ±1.36 against −184.29°).

Fixes: the layers belong to the idles (§5.2), and sad and surprised keep their legs canonical in
posture (0.6–0.8° of leg splay is not visible at this size; their sway still scales).

Measured through the real library (`NativeRigIntensityTest`: every expression at every intensity,
entry from Default, regular idle, long idle, idle again; the full report prints with the test):

| | Before | After |
|---|---|---|
| Sessions with any intervention | 32 of 70 | 1 of 70 |
| Largest, entry and regular idle | 0.824 (surprised 10, `hand_l` rotation) | 0.0008 |
| Largest, including the long idle | 2.498 (sleepy 10, `head` rotation) | 0.0050 |
| Where | entries and long idles in six states; sad's and surprised's idles (`leg_r`) | sad 10 only: `forearm_l`/`forearm_r` rotation, 14 frames |

The remainder is sad at 10: its forearms are held in posture, and while `idle_sad` crossfades with
its neighbours the scaled sway of the outgoing idle adds a few thousandths of a degree on top of a
blend that sits at the forearms' edge. Applied/requested is 0.99997. The test bounds every
intervention at 0.01; neutral intensities are never clamped.

### 5.5 The hood and sleeve seam

The hood is one image on the `head` bone, drawn over the torso; the sleeves (`arm_l`, `arm_r`,
meshes weighted to chest, upper arm and forearm) are drawn over it. Each sleeve's white sticker
border runs down its outer side and **ends in a straight cut at the shoulder**. Laid over the
hood's own white border it merges; over the grey fur it is a hard white edge.

At sleepy 10 the posture layer turned the head 2.5° further (sleepy holds its head 5° from
Default's), while sleepy's shoulders stayed canonical — they must: `upperarm_l` already sits at its
authored extreme (−156.8°, the range minimum), so it cannot follow. The relative turn slid the left
sleeve's cut border from the hood's white border onto the fur. Isolated by rendering sleepy 10 with
one bone group at a time held: only the head reproduces it; forearms and hands do not. The
authored long idle passes through the same contact for a moment (up to 30 px in the probe below);
the layer held it there for as long as the idle played.

Fix: sleepy keeps its head canonical in the posture layer (the profile's `hold_canonical`). No
asset, skeleton, atlas or engine change; the expression keeps its body, chest, forearm and hand
emphasis. A fractional head weight did not help: even 0.2 of the head's offset already exceeds the
canonical seam at 10.

The probe (`NativeRigSeamTest`) renders the real library's triangles over the atlas page and counts
the pixels where a sleeve's white border lies over grey fur, over one settled idle loop (mean and
largest frame), against the canonical idle. Before → after for sleepy:

| Sleepy | 1 | 2 | 5 (canonical) | 8 | 10 |
|---|---|---|---|---|---|
| Before (mean/max) | 1/2 | 1/2 | 4/14 | 9/18 | 12/21 |
| After | 4/5 | 4/5 | 4/14 | 5/16 | 5/15 |

(The diagnostic renderer's bilinear figures; the test's nearest-sampled ones differ slightly and
are compared with its own canonical.) Every other expression stays within 2 px of its canonical
mean at 1 and 10. One passing peak is recorded rather than fixed: **satisfied at 10** shows a short
straight stretch of sleeve border over the fur for about half a second at the top of its loop (9 px
against canonical's 7; 13 against 6 in the test's sampling), from satisfied's own head tilt (+0.9°
at 10), which is most of its posture emphasis. The test pins it so it cannot grow; whether to keep
it is a calibration call.

### 5.6 Performance

Reference data (held poses, the reference and the range) is computed once at load. A frame with an
active layer samples at most two idles into a scratch pose and recomputes the world transforms once
more — no allocation (`tests/allocation.rs`; the per-bone statistics are allocated at load), no JNI
call per frame, the same geometry buffers, the same single draw. At neutral nothing extra runs.

### 5.7 Tests and acceptance

- Kotlin: `IntensityPolicyTest` (shared vectors, the threshold table, baselines, dead band,
  settings), controller and presentation intensity cases (the dead band is "at least 2": a
  threshold-7 state at score 7 then 8 moves from 4 to 6), `RigIntensityProfileTest`,
  `RigCharacterPlayerIntensityTest` (gains only on change, no restart, exit/entrance, reduced
  motion), `RigAssetsTest` (applied at load; missing, invalid or refused → canonical),
  `CharacterViewTest` (images ignore intensity; an intensity change restarts nothing).
- Real library: `NativeRigIntensityTest` (applies; neutral bit-identical; every expression at every
  intensity changes the pose exactly when a gain is not 1 and never needs the guard beyond 0.01;
  no jumps; bad values and calls after release refused) and `NativeRigSeamTest` (§5.5, including
  that the probe sees the defect when sleepy's head hold is removed).
- Rust: `tests/shape.rs` (neutral bit-identical in all 28 animations, exact posture and motion
  scaling, held bones, loop closure and held-pose preservation, long idles bit-identical once their
  crossfade is done and posture easing back after, the profile's extreme gains never needing the
  guard, attachments and draw order identical, continuity, ramps, guard, validation) and
  `tests/allocation.rs`.
- Python: `tests/presentation/test_presentation_intensity.py` (same vectors as Kotlin).
- Device: `CharacterRigDeviceTest` renders every expression at intensities 1, 5 and 10 (silhouette
  overlap with the same moment at 5 above 0.85, idle motion rising with intensity), further levels
  (sleepy 2 and 8, angry 1, 4 and 10, sad 10), the regular idle into its long idle and back, and
  intensity changing during an expression, plus reduced motion, pause/resume and the static
  fallback; frames and contact sheets are saved under the app's `files/rig-frames/intensity/`.

Visual acceptance, for the owner's review of those sheets on a device: every expression stays
recognisable; sad stays controlled at 10; angry never looks hostile or shaky; sleepy at 2 is calm,
not frozen; surprised stays recognisable despite its canonical entry; no mesh tearing, joint
separation or neck gap; no abrupt movement when intensity changes, and no jump at idle, transition
or long-idle boundaries. Emulator captures do not establish how a physical device renders.
