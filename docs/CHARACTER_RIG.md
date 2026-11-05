# The animated character on Android (Rigby2D rig)

How the Android client draws the skeletal JARVIS character instead of the seven static images,
and what to know before changing it. The character is still presentation only
(`docs/CHARACTER_PRESENTATION.md`): `CharacterController` decides the expression; this renders it.

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
| Native library | `android/rig-native/` | `rigby-runtime/`: the Rigby2D Rust runtime, vendored unmodified (`UPSTREAM.md`). `jarvis-rig/`: the JNI bridge (`rig.rs` core, `jni.rs` entry points) |
| Contract | `rig/RigContract.kt` | Parses the control contract; refuses a malformed or incomplete one whole |
| Player | `rig/CharacterAnimationPlayer.kt`, `rig/RigCharacterPlayer.kt` | Expression → animation, per the contract (below) |
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
  still, redrawn only when the state changes. No entrance.
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

`NativeRigRuntimeTest` runs the real library (host build) from plain JUnit; a native library binds
to one class loader, so Robolectric tests never load it and always exercise the fallback. When the
build is native (the default), a missing host library fails that test rather than skipping it.

To update the character: re-export from the rig project, copy the four files into
`assets/character/rig/`, regenerate `SHA256SUMS`, and run the tests above. The contract and the
skeleton must agree, or the player refuses the rig.
