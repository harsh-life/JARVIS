//! JNI entry points for `com.hypermind.jarvis.presentation.character.rig.RigNative`.
//!
//! Ownership: `nativeCreate` returns a `Box<Rig>` as a raw pointer (the handle); only
//! `nativeDestroy` frees it, and the Kotlin side guarantees one destroy and no use after it. No
//! JNI reference outlives a call: nothing is stored but the handle, and only primitive arrays
//! cross the boundary (no strings, objects, global references or callbacks).
//!
//! Every array length is checked before it is written, so no Java exception is ever left pending,
//! and every entry point catches panics, so a bug in the runtime reports a failure instead of
//! unwinding into the JVM.
//!
//! # Safety
//!
//! Every function here is called only by the JVM, through `RigNative`: `env` is the calling
//! thread's valid `JNIEnv`, array arguments are null or live local references of the declared
//! type, and `handle` is 0 or a pointer from `nativeCreate` not yet passed to `nativeDestroy`.
#![allow(clippy::missing_safety_doc)]

use std::panic::{catch_unwind, AssertUnwindSafe};
use jni_sys::{jboolean, jbyteArray, jclass, jfloat, jfloatArray, jint, jintArray, jlong, jshortArray, jsize, JNIEnv, JNI_FALSE, JNI_TRUE};

use crate::rig::Rig;

unsafe fn array_len(env: *mut JNIEnv, array: jni_sys::jarray) -> usize {
    if array.is_null() {
        return 0;
    }
    ((**env).GetArrayLength.unwrap())(env, array).max(0) as usize
}

unsafe fn read_bytes(env: *mut JNIEnv, array: jbyteArray) -> Vec<u8> {
    let n = array_len(env, array);
    let mut out = vec![0u8; n];
    if n > 0 {
        ((**env).GetByteArrayRegion.unwrap())(env, array, 0, n as jsize, out.as_mut_ptr().cast());
    }
    out
}

/// Reads a whole int array (configuration only: never per frame).
unsafe fn read_ints(env: *mut JNIEnv, array: jintArray) -> Vec<i32> {
    let n = array_len(env, array);
    let mut out = vec![0i32; n];
    if n > 0 {
        ((**env).GetIntArrayRegion.unwrap())(env, array, 0, n as jsize, out.as_mut_ptr());
    }
    out
}

/// Reads a whole float array (configuration only: never per frame).
unsafe fn read_floats(env: *mut JNIEnv, array: jfloatArray) -> Vec<f32> {
    let n = array_len(env, array);
    let mut out = vec![0f32; n];
    if n > 0 {
        ((**env).GetFloatArrayRegion.unwrap())(env, array, 0, n as jsize, out.as_mut_ptr());
    }
    out
}

/// Non-negative ints as indices, or None if any is negative.
fn indices(values: &[i32]) -> Option<Vec<usize>> {
    values.iter().map(|&v| usize::try_from(v).ok()).collect()
}

unsafe fn write_floats(env: *mut JNIEnv, array: jfloatArray, values: &[f32]) -> bool {
    if values.len() > array_len(env, array) {
        return false;
    }
    if !values.is_empty() {
        ((**env).SetFloatArrayRegion.unwrap())(env, array, 0, values.len() as jsize, values.as_ptr());
    }
    true
}

unsafe fn write_ints(env: *mut JNIEnv, array: jintArray, values: &[i32]) -> bool {
    if values.len() > array_len(env, array) {
        return false;
    }
    if !values.is_empty() {
        ((**env).SetIntArrayRegion.unwrap())(env, array, 0, values.len() as jsize, values.as_ptr());
    }
    true
}

unsafe fn write_shorts(env: *mut JNIEnv, array: jshortArray, values: &[u16]) -> bool {
    if values.len() > array_len(env, array) {
        return false;
    }
    if !values.is_empty() {
        ((**env).SetShortArrayRegion.unwrap())(env, array, 0, values.len() as jsize, values.as_ptr().cast());
    }
    true
}

/// The rig behind a handle, or None for 0.
unsafe fn rig<'a>(handle: jlong) -> Option<&'a mut Rig> {
    (handle as *mut Rig).as_mut()
}

fn guarded<T>(fallback: T, f: impl FnOnce() -> T) -> T {
    catch_unwind(AssertUnwindSafe(f)).unwrap_or(fallback)
}

const fn flag(b: bool) -> jboolean {
    if b {
        JNI_TRUE
    } else {
        JNI_FALSE
    }
}

/// Loads a rig. Returns its handle, or 0 with a UTF-8 message written into `error` (truncated to
/// fit; the rest zeroed).
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeCreate(
    env: *mut JNIEnv,
    _class: jclass,
    skeleton: jbyteArray,
    atlas: jbyteArray,
    error: jbyteArray,
) -> jlong {
    let result = guarded(Err("the runtime panicked while loading".to_string()), || {
        let skeleton = read_bytes(env, skeleton);
        let atlas = read_bytes(env, atlas);
        let atlas = std::str::from_utf8(&atlas).map_err(|_| "atlas: not UTF-8".to_string())?;
        Rig::load(&skeleton, atlas)
    });
    match result {
        Ok(rig) => Box::into_raw(Box::new(rig)) as jlong,
        Err(message) => {
            let room = array_len(env, error);
            let mut bytes = vec![0u8; room];
            let m = message.as_bytes();
            let n = m.len().min(room);
            bytes[..n].copy_from_slice(&m[..n]);
            if room > 0 {
                ((**env).SetByteArrayRegion.unwrap())(env, error, 0, room as jsize, bytes.as_ptr().cast());
            }
            0
        }
    }
}

#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeDestroy(
    _env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
) {
    if handle != 0 {
        // A panic in a destructor would only leak; never unwind into the JVM.
        let _ = catch_unwind(|| drop(Box::from_raw(handle as *mut Rig)));
    }
}

/// `[vertex capacity, index capacity, animation count, min x, min y, max x, max y, page width,
/// page height, bone count]`; false if `out` is too short.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeInfo(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    out: jfloatArray,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    let values = guarded(None, || {
        let b = rig.bounds();
        let p = rig.page_size();
        Some([rig.vertex_capacity() as f32, rig.index_capacity() as f32, rig.animation_count() as f32, b[0], b[1], b[2], b[3], p[0], p[1], rig.bone_count() as f32])
    });
    flag(values.is_some_and(|v| write_floats(env, out, &v)))
}

/// The index of the animation named by the UTF-8 bytes `name`, or -1.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeAnimationIndex(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    name: jbyteArray,
) -> jint {
    let Some(rig) = rig(handle) else { return -1 };
    let name = read_bytes(env, name);
    guarded(-1, || std::str::from_utf8(&name).ok().and_then(|n| rig.animation_index(n)).map_or(-1, |i| i as jint))
}

/// The duration of animation `index` in seconds, or -1.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeAnimationDuration(
    _env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    index: jint,
) -> jfloat {
    let Some(rig) = rig(handle) else { return -1.0 };
    if index < 0 {
        return -1.0;
    }
    guarded(-1.0, || rig.animation_duration(index as usize).map_or(-1.0, |d| d as f32))
}

#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeSetAnimation(
    _env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    track: jint,
    index: jint,
    looping: jboolean,
    mix: jfloat,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    if track < 0 || index < 0 {
        return JNI_FALSE;
    }
    flag(guarded(false, || rig.set_animation(track as usize, index as usize, looping == JNI_TRUE, mix as f64)))
}

#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeAddAnimation(
    _env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    track: jint,
    index: jint,
    looping: jboolean,
    mix: jfloat,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    if track < 0 || index < 0 {
        return JNI_FALSE;
    }
    flag(guarded(false, || rig.add_animation(track as usize, index as usize, looping == JNI_TRUE, mix as f64)))
}

#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeClearTrack(
    _env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    track: jint,
) {
    if let Some(rig) = rig(handle) {
        if track >= 0 {
            guarded((), || rig.clear_track(track as usize));
        }
    }
}

/// `[current animation (-1 when empty), its time, entries queued]` for `track`.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeTrack(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    track: jint,
    out: jfloatArray,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    if track < 0 {
        return JNI_FALSE;
    }
    let t = track as usize;
    let values = guarded(None, || Some([rig.current(t).map_or(-1.0, |a| a as f32), rig.current_time(t) as f32, rig.queued(t) as f32]));
    flag(values.is_some_and(|v| write_floats(env, out, &v)))
}

/// Advances by `dt` seconds and writes the track callbacks since the last update into `events` as
/// `[kind, track, animation]` triples. Returns how many triples were written (as many as fit), or
/// -1 on failure.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeUpdate(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    dt: jfloat,
    events: jintArray,
) -> jint {
    let Some(rig) = rig(handle) else { return -1 };
    if !guarded(false, || {
        rig.update(dt as f64);
        true
    }) {
        return -1;
    }
    let room = array_len(env, events) / 3;
    let flat = rig.take_events(room);
    if write_ints(env, events, flat) {
        (flat.len() / 3) as jint
    } else {
        -1
    }
}

/// Builds the current pose's geometry into `positions` (x, y), `texs` (page pixels) and
/// `indices`, and writes `[vertex count, index count]` into `counts`. False if anything does not
/// fit (the arrays must hold the capacities from `nativeInfo`).
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeBuild(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    positions: jfloatArray,
    texs: jfloatArray,
    indices: jshortArray,
    counts: jintArray,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    if !guarded(false, || rig.build().is_ok()) {
        return JNI_FALSE;
    }
    let ok = write_floats(env, positions, &rig.positions)
        && write_floats(env, texs, &rig.texs)
        && write_shorts(env, indices, &rig.indices)
        && write_ints(env, counts, &[(rig.positions.len() / 2) as i32, rig.indices.len() as i32]);
    flag(ok)
}

/// The index of the bone named by the UTF-8 bytes `name`, or -1.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeBoneIndex(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    name: jbyteArray,
) -> jint {
    let Some(rig) = rig(handle) else { return -1 };
    let name = read_bytes(env, name);
    guarded(-1, || std::str::from_utf8(&name).ok().and_then(|n| rig.bone_index(n)).map_or(-1, |i| i as jint))
}

/// Configures the presentation-intensity layers (`Shape::configure`): `idles` and `range` are
/// animation indices, the weights one value per bone for each idle. Called once, while loading.
/// False, leaving any previous configuration, if anything is invalid.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeConfigureShape(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    reference: jint,
    idles: jintArray,
    range: jintArray,
    posture_weights: jfloatArray,
    motion_weights: jfloatArray,
    posture_ramp: jfloat,
    motion_ramp: jfloat,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    let (Ok(reference), Some(idles), Some(range)) = (usize::try_from(reference), indices(&read_ints(env, idles)), indices(&read_ints(env, range))) else {
        return JNI_FALSE;
    };
    let posture = read_floats(env, posture_weights);
    let motion = read_floats(env, motion_weights);
    flag(guarded(false, || rig.configure_shape(reference, &idles, &range, &posture, &motion, posture_ramp as f64, motion_ramp as f64).is_ok()))
}

/// Ramps to the gains of idle animation `idle` (-1: every gain to 1, the exit). No arrays; called
/// only when the shown expression or intensity changes, never per frame.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeSetShape(
    _env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    idle: jint,
    posture: jfloat,
    motion: jfloat,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    if idle < -1 {
        return JNI_FALSE;
    }
    let idle = usize::try_from(idle).ok();
    flag(guarded(false, || rig.set_shape(idle, posture as f64, motion as f64)))
}

/// `[configured (0/1), active (0/1), guard clamps, largest clamp, its bone (-1 for none), its
/// channel, requested total, applied total]` (see `GuardStats`); false if `out` is too short.
#[no_mangle]
pub unsafe extern "system" fn Java_com_hypermind_jarvis_presentation_character_rig_RigNative_nativeShapeInfo(
    env: *mut JNIEnv,
    _class: jclass,
    handle: jlong,
    out: jfloatArray,
) -> jboolean {
    let Some(rig) = rig(handle) else { return JNI_FALSE };
    let values = guarded(None, || {
        Some(match rig.shape() {
            None => [0.0, 0.0, 0.0, 0.0, -1.0, -1.0, 0.0, 0.0],
            Some(s) => {
                let g = s.stats();
                let (bone, channel) = if g.clamps > 0 { (g.largest_bone as f32, g.largest_channel as f32) } else { (-1.0, -1.0) };
                [1.0, if s.active() { 1.0 } else { 0.0 }, g.clamps as f32, g.largest as f32, bone, channel, g.requested as f32, g.applied as f32]
            }
        })
    });
    flag(values.is_some_and(|v| write_floats(env, out, &v)))
}
