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
- **Idle motion** adds `(motion gain − 1) × (idle pose at its time − held pose)`, only for the
  regular idles on track 0, weighted by each one's crossfade share.

They are independent internally; the profile gives each its own curve. Every gain ramps
(smoothstep: posture 0.8 s, motion 0.5 s). With both gains exactly 1 — intensities 5 and 6 — the
native side touches nothing and the output is bit-identical to the canonical animation.

### 5.2 What stays canonical

- The **animation assets**: skeleton, atlas, page, contract and every clip are unchanged.
- **Idle motion** scales only regular idles: long idles, entries, pair transitions, the entrance and
  the exit keep their authored motion.
- **Faces, blinks and draw order**: the layers only move bones; attachments never change.
- **Reduced motion, the static fallback and the software-rendering fallback** are unchanged.

**The rendered pose during a transition is not strictly canonical.** The posture layer applies
whatever plays, so when the expression changes its contribution retargets smoothly while the
transition runs (instead of jumping when a clip ends), and it continues through long idles. The
exit ramps both layers back to 1; the entrance re-applies them.

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
                  "hold_canonical": ["upperarm_r", "upperarm_l", "forearm_r", "forearm_l", "face"]},
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
| sleepy | 1.50 | 1.40 | shoulders, face | face |
| surprised | 1.25 | 1.70 | shoulders, face | face |
| angry | 1.30 | 1.35 | shoulders, hands, face | hands, face |
| sad | 1.40 | 1.40 | shoulders, forearms, face | face |

Below neutral every state uses posture 0.60/0.65/0.78/0.90 and motion 0.25/0.35/0.55/0.75 for
intensities 1–4 (Default's posture is 1 throughout). Above neutral, intensities 7–10 reach 30, 55,
80 and 100 % of the way to the top. These are the provisional values from the feasibility
investigation, checked geometrically against the exported rig — **not visually calibrated**.

Surprised draws much of its force from its entry transition, which stays canonical; its intensity
shows in its held reaction and idle only.

### 5.4 The authored-range guard

Each adjusted channel is kept within the range the rig's own track-0 animations reach for it
(sampled at load), never tighter than that frame's canonical value — so it only ever limits what a
layer added, and never clamps the canonical animation. It counts what it clamps
(`NativeRigRuntime.shapeInfo`), so tests can tell a trimmed peak from flattened behaviour. It is a
safety boundary, not a substitute for calibrating the profile.

### 5.5 Performance

Reference data (held poses, the reference and the range) is computed once at load. A frame with an
active layer samples at most two idles into a scratch pose and recomputes the world transforms once
more — no allocation (`tests/allocation.rs`), no JNI call per frame, the same geometry buffers, the
same single draw. At neutral nothing extra runs.

### 5.6 Open: the guard during long idles

Measured by `NativeRigIntensityTest` (every expression at every intensity, through entry, idle,
long idle and idle again), the largest amount the guard trimmed, in degrees or units:

| State | Entry and regular idle | Including the long idle |
|---|---:|---:|
| happy | 0.65 | 1.00 |
| satisfied | 0.36 | 0.91 |
| default | 0 | 0 |
| sleepy | 0.30 | 2.50 |
| surprised | 0.82 | 0.99 |
| angry | 0.39 | 1.00 |
| sad | 0.54 | 1.60 |

Through the entry and regular idle — what the profile was checked against — the guard only trims
rare peaks. During long idles the posture layer is added on top of motion whose authored peaks
already reach the rig's range, so there it trims up to 2.5 (sleepy at 10), and angry's posture
below neutral reaches 1.0 too. The test pins these maxima so they cannot grow unnoticed. Whether to
accept this, lower the provisional gains, or fade posture during long idles is an owner decision;
until then the gains stay provisional.

Visual evidence (emulator, `jarvis_rig_test`, arm64 API 35; `CharacterRigDeviceTest` contact
sheets): every expression stays recognisable at 1, 5 and 10, and the face is unchanged. The
difference is subtle in stills (mostly head tilt; Default differs only in motion). One artefact
is caused by intensity: at sleepy 10 the larger head tilt opens a hard edge where the hood meets
the right shoulder and the sticker's white border shows through, which it does not at 5. A thin
outline sliver beside sad's left hand is present at 5 as well, so it belongs to the canonical
animation. Not yet seen on a physical device. The visual calibration is not done.

### 5.7 Tests and acceptance

- Kotlin: `IntensityPolicyTest` (shared vectors, the threshold table, baselines, dead band,
  settings), controller and presentation intensity cases, `RigIntensityProfileTest`,
  `RigCharacterPlayerIntensityTest` (gains only on change, no restart, exit/entrance, reduced
  motion), `RigAssetsTest` (applied at load; missing, invalid or refused → canonical),
  `CharacterViewTest` (images ignore intensity; an intensity change restarts nothing).
- Real library: `NativeRigIntensityTest` (applies; neutral bit-identical; every expression at every
  intensity, pose changes exactly when a gain is not 1, guard bounds of §5.6; no jumps; bad values
  and calls after release refused).
- Rust: `tests/shape.rs` (neutral bit-identical in all 28 animations, exact posture and motion
  scaling, held bones, loop closure and held-pose preservation, authored clips untouched,
  attachments and draw order identical, continuity, ramps, guard, validation) and
  `tests/allocation.rs`.
- Python: `tests/presentation/test_presentation_intensity.py` (same vectors as Kotlin).
- Device: `CharacterRigDeviceTest` renders every expression at intensities 1, 5 and 10 (silhouette
  overlap with the same moment at 5 above 0.85, idle motion rising with intensity), reduced motion,
  pause/resume, and the static fallback; frames and contact sheets are saved under the app's
  `files/rig-frames/intensity/`.

Visual acceptance, for the owner's review of those sheets on a device: every expression stays
recognisable; sad stays controlled at 10; angry never looks hostile or shaky; sleepy at 2 is calm,
not frozen; surprised stays recognisable despite its canonical entry; no mesh tearing, joint
separation or neck gap; no abrupt movement when intensity changes, and no jump at idle, transition
or long-idle boundaries. Emulator captures do not establish how a physical device renders.
