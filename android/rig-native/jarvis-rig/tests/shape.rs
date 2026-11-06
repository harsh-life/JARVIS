//! The presentation-intensity layers (`shape`) against the character the app bundles: neutral is
//! bit-identical, posture and motion scale exactly what they should and nothing else, held bones
//! stay canonical, the guard only limits what was added, attachments never change, nothing jumps,
//! and gains ramp.

use jarvis_rig::rig::Rig;
use jarvis_rig::shape::{CHANNELS, MAX_GAIN};

const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../assets/character/rig");
const STATES: [&str; 7] = ["happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad"];
const FRAME: f64 = 1.0 / 60.0;
const POSTURE_RAMP: f64 = 0.8;
const MOTION_RAMP: f64 = 0.5;
/// Bones held canonical in the posture layer, per state (the intensity profile's choice), plus
/// the face everywhere.
const POSTURE_HOLDS: [&[&str]; 7] = [
    &[],
    &[],
    &[],
    &["upperarm_r", "upperarm_l"],
    &["upperarm_r", "upperarm_l"],
    &["upperarm_r", "upperarm_l", "hand_r", "hand_l"],
    &["upperarm_r", "upperarm_l", "forearm_r", "forearm_l"],
];
const MOTION_HOLDS: [&[&str]; 7] = [&[], &[], &[], &[], &[], &["hand_r", "hand_l"], &[]];

fn rig() -> Rig {
    let skel = std::fs::read(format!("{DIR}/JarvisCharacter.skel")).expect("bundled skeleton");
    let atlas = std::fs::read_to_string(format!("{DIR}/JarvisCharacter.atlas")).expect("bundled atlas");
    Rig::load(&skel, &atlas).expect("the bundled rig loads")
}

fn index(rig: &Rig, name: &str) -> usize {
    rig.animation_index(name).unwrap_or_else(|| panic!("{name} missing"))
}

fn idles(rig: &Rig) -> Vec<usize> {
    STATES.iter().map(|s| index(rig, &format!("idle_{s}"))).collect()
}

fn idle(rig: &Rig, state: &str) -> usize {
    index(rig, &format!("idle_{state}"))
}

/// Every animation but the overlay's entrance and exit.
fn range(rig: &Rig) -> Vec<usize> {
    let overlay = [index(rig, "enter"), index(rig, "exit")];
    (0..rig.animation_count()).filter(|i| !overlay.contains(i)).collect()
}

fn bone(rig: &Rig, name: &str) -> usize {
    rig.bone_index(name).unwrap_or_else(|| panic!("bone {name} missing"))
}

fn weights(rig: &Rig, holds: &[&[&str]; 7]) -> Vec<f32> {
    let face = bone(rig, "face");
    let mut out = Vec::new();
    for held in holds {
        let held: Vec<usize> = held.iter().map(|n| bone(rig, n)).collect();
        out.extend((0..rig.bone_count()).map(|b| if b == face || held.contains(&b) { 0.0 } else { 1.0 }));
    }
    out
}

/// A rig with the profile's held bones, configured as the app does.
fn configured() -> Rig {
    let mut r = rig();
    let (i, rg) = (idles(&r), range(&r));
    let (pw, mw) = (weights(&r, &POSTURE_HOLDS), weights(&r, &MOTION_HOLDS));
    let reference = idle(&r, "default");
    r.configure_shape(reference, &i, &rg, &pw, &mw, POSTURE_RAMP, MOTION_RAMP).expect("configures");
    r
}

/// Applies the same playback call to every rig.
fn both(rigs: &mut [&mut Rig], f: impl Fn(&mut Rig)) {
    for r in rigs.iter_mut() {
        f(r);
    }
}

fn frames(rigs: &mut [&mut Rig], seconds: f64) {
    for _ in 0..(seconds / FRAME).round() as usize {
        both(rigs, |r| r.update(FRAME));
    }
}

#[test]
fn neutral_gains_are_bit_identical_in_every_animation() {
    let mut plain = rig();
    let mut unset = configured();
    let mut neutral = configured();
    let happy = idle(&neutral, "happy");
    assert!(neutral.set_shape(Some(happy), 1.0, 1.0));
    for anim in 0..plain.animation_count() {
        let d = plain.animation_duration(anim).unwrap();
        both(&mut [&mut plain, &mut unset, &mut neutral], |r| {
            r.set_animation(0, anim, true, 0.0);
        });
        for _ in 0..((d + 0.5) / FRAME) as usize {
            both(&mut [&mut plain, &mut unset, &mut neutral], |r| {
                r.update(FRAME);
                r.build().unwrap();
            });
            assert_eq!(plain.positions, unset.positions, "animation {anim}");
            assert_eq!(plain.positions, neutral.positions, "animation {anim}");
        }
    }
    assert!(!neutral.shape().unwrap().active());
    assert_eq!(neutral.shape().unwrap().stats().clamps, 0);
}

#[test]
fn a_gain_that_returns_to_neutral_returns_to_the_exact_canonical_output() {
    let mut plain = rig();
    let mut shaped = configured();
    let sad = idle(&plain, "sad");
    both(&mut [&mut plain, &mut shaped], |r| {
        r.set_animation(0, sad, true, 0.0);
    });
    shaped.set_shape(Some(sad), 1.4, 1.4);
    frames(&mut [&mut plain, &mut shaped], 1.5);
    shaped.set_shape(Some(sad), 1.0, 1.0);
    frames(&mut [&mut plain, &mut shaped], POSTURE_RAMP.max(MOTION_RAMP) + 0.1);
    assert!(!shaped.shape().unwrap().active());
    for _ in 0..120 {
        both(&mut [&mut plain, &mut shaped], |r| {
            r.update(FRAME);
            r.build().unwrap();
        });
        assert_eq!(plain.positions, shaped.positions);
    }
}

#[test]
fn the_reference_and_held_poses_come_from_the_exported_idles() {
    let r = configured();
    let setup: Vec<[f64; CHANNELS]> = (0..r.bone_count()).map(|b| r.local(b).unwrap()).collect();
    let shape = r.shape().unwrap();
    // Default's held pose is (to within its sampling) the setup pose: computed, not assumed.
    for (b, reference) in shape.reference().iter().enumerate() {
        for c in 0..CHANNELS {
            assert!((reference[c] - setup[b][c]).abs() < 0.02, "bone {b} channel {c}: {} vs {}", reference[c], setup[b][c]);
        }
    }
    // Sad holds its head about 3 degrees down and its body about 2 units lower.
    let sad = shape.held(idle(&r, "sad")).unwrap();
    let head = bone(&r, "head");
    assert!((sad[head][2] - setup[head][2] + 3.0).abs() < 0.05, "{}", sad[head][2] - setup[head][2]);
    let body = bone(&r, "body");
    assert!((sad[body][1] - setup[body][1] + 2.0).abs() < 0.05, "{}", sad[body][1] - setup[body][1]);
}

#[test]
fn posture_adds_the_scaled_held_offset_and_holds_its_excluded_bones() {
    let mut plain = rig();
    let mut shaped = configured();
    let sad = idle(&plain, "sad");
    both(&mut [&mut plain, &mut shaped], |r| {
        r.set_animation(0, sad, true, 0.0);
    });
    shaped.set_shape(Some(sad), 1.4, 1.0);
    frames(&mut [&mut plain, &mut shaped], POSTURE_RAMP + 0.1);
    let held = shaped.shape().unwrap().held(sad).unwrap().to_vec();
    let reference = shaped.shape().unwrap().reference().to_vec();
    let kept: Vec<usize> = ["upperarm_r", "upperarm_l", "forearm_r", "forearm_l", "face"].iter().map(|n| bone(&plain, n)).collect();
    for _ in 0..200 {
        frames(&mut [&mut plain, &mut shaped], FRAME);
        for b in 0..plain.bone_count() {
            let (p, s) = (plain.local(b).unwrap(), shaped.local(b).unwrap());
            for c in 0..CHANNELS {
                if kept.contains(&b) {
                    assert_eq!(p[c], s[c], "held bone {b} channel {c} stays canonical");
                } else {
                    let added = 0.4 * (held[b][c] - reference[b][c]);
                    let added = if added.abs() < 1e-9 { 0.0 } else { added };
                    // The guard may limit a rare peak (never past the canonical value).
                    let (low, high) = shaped.shape().unwrap().range(b).unwrap();
                    let expected = (p[c] + added).clamp(low[c].min(p[c]), high[c].max(p[c]));
                    assert!((s[c] - expected).abs() < 1e-9, "bone {b} channel {c}: {} vs {}", s[c], expected);
                }
            }
        }
    }
    // At 1.4 the guard only trims sad's right leg at the top of its sway, by a twentieth of a
    // degree: the 2-degree held leg plus 0.8 more reaches past anything the rig authors there.
    let stats = shaped.shape().unwrap().stats();
    assert_eq!(stats.largest_bone, bone(&plain, "leg_r"), "{stats:?}");
    assert!(stats.largest < 0.1, "{stats:?}");
    // The head goes further down: about 1.2 degrees more at 1.4.
    let head = bone(&plain, "head");
    let extra = shaped.local(head).unwrap()[2] - plain.local(head).unwrap()[2];
    assert!((-1.3..-1.1).contains(&extra), "{extra}");
}

#[test]
fn motion_scales_the_deviation_around_the_held_pose_and_keeps_the_loop() {
    let mut plain = rig();
    let mut shaped = configured();
    let happy = idle(&plain, "happy");
    both(&mut [&mut plain, &mut shaped], |r| {
        r.set_animation(0, happy, true, 0.0);
    });
    shaped.set_shape(Some(happy), 1.0, 1.5);
    frames(&mut [&mut plain, &mut shaped], MOTION_RAMP + 0.1);
    let held = shaped.shape().unwrap().held(happy).unwrap().to_vec();
    let face = bone(&plain, "face");
    let period = (plain.animation_duration(happy).unwrap() / FRAME).round() as usize;
    let mut first = Vec::new();
    let mut mean = vec![[0.0; CHANNELS]; plain.bone_count()];
    for k in 0..=period {
        frames(&mut [&mut plain, &mut shaped], FRAME);
        for b in 0..plain.bone_count() {
            let (p, s) = (plain.local(b).unwrap(), shaped.local(b).unwrap());
            for c in 0..CHANNELS {
                if b == face {
                    assert_eq!(p[c], s[c]);
                    continue;
                }
                let expected = held[b][c] + 1.5 * (p[c] - held[b][c]);
                assert!((s[c] - expected).abs() < 1e-9, "bone {b} channel {c}");
                if k < period {
                    mean[b][c] += s[c] / period as f64;
                }
            }
        }
        if k == 0 {
            first = (0..plain.bone_count()).map(|b| shaped.local(b).unwrap()).collect();
        }
        if k == period {
            // One loop later the scaled pose is the same: the loop still closes.
            for (b, f) in first.iter().enumerate() {
                let now = shaped.local(b).unwrap();
                for c in 0..CHANNELS {
                    assert!((now[c] - f[c]).abs() < 1e-6, "bone {b} channel {c}");
                }
            }
        }
    }
    // The held pose is preserved: the scaled loop averages to it.
    for b in (0..plain.bone_count()).filter(|&b| b != face) {
        for c in 0..CHANNELS {
            assert!((mean[b][c] - held[b][c]).abs() < 0.02, "bone {b} channel {c}: {} vs {}", mean[b][c], held[b][c]);
        }
    }
}

#[test]
fn transitions_long_idles_and_the_overlay_keep_their_authored_motion() {
    let mut plain = rig();
    let mut shaped = configured();
    let happy = idle(&plain, "happy");
    // Motion only (posture neutral): nothing but a regular idle may change.
    shaped.set_shape(Some(happy), 1.0, 1.7);
    for name in ["to_happy", "long_idle_happy", "trans_happy_to_satisfied"] {
        let a = index(&plain, name);
        both(&mut [&mut plain, &mut shaped], |r| {
            r.set_animation(0, a, false, 0.0);
        });
        for _ in 0..((plain.animation_duration(a).unwrap() - 0.05) / FRAME) as usize {
            both(&mut [&mut plain, &mut shaped], |r| {
                r.update(FRAME);
                r.build().unwrap();
            });
            assert_eq!(plain.positions, shaped.positions, "{name} is authored motion");
        }
    }
    // The entrance (track 1, body scale) over a scaled idle keeps its own scale exactly.
    let enter = index(&plain, "enter");
    both(&mut [&mut plain, &mut shaped], |r| {
        r.set_animation(0, happy, true, 0.0);
        r.set_animation(1, enter, false, 0.0);
    });
    let body = bone(&plain, "body");
    for _ in 0..40 {
        frames(&mut [&mut plain, &mut shaped], FRAME);
        let (p, s) = (plain.local(body).unwrap(), shaped.local(body).unwrap());
        assert_eq!((p[3], p[4]), (s[3], s[4]), "the entrance's scale is untouched");
    }
}

#[test]
fn faces_blinks_and_draw_order_never_change() {
    let mut plain = rig();
    let mut shaped = configured();
    let ids = idles(&plain);
    let tops = [(1.5, 1.7), (1.35, 1.4), (1.0, 1.3), (1.5, 1.4), (1.25, 1.7), (1.3, 1.35), (1.4, 1.4)];
    for (i, state) in STATES.iter().enumerate() {
        let to = index(&plain, &format!("to_{state}"));
        let long = index(&plain, &format!("long_idle_{state}"));
        both(&mut [&mut plain, &mut shaped], |r| {
            r.set_animation(0, to, false, 0.3);
            r.add_animation(0, ids[i], true, 0.2);
            r.add_animation(0, long, false, 0.3);
            r.add_animation(0, ids[i], true, 0.4);
        });
        shaped.set_shape(Some(ids[i]), tops[i].0, tops[i].1);
        let seconds = plain.animation_duration(to).unwrap() + plain.animation_duration(ids[i]).unwrap() + plain.animation_duration(long).unwrap() + 1.0;
        for _ in 0..(seconds / FRAME) as usize {
            both(&mut [&mut plain, &mut shaped], |r| {
                r.update(FRAME);
                r.build().unwrap();
            });
            // Which images show, in what order, with which texture coordinates.
            assert_eq!(plain.texs, shaped.texs, "{state}");
            assert_eq!(plain.indices, shaped.indices, "{state}");
        }
    }
}

/// The largest distance any vertex moves in one frame, over a sequence.
fn largest_step(r: &mut Rig, run: &mut dyn FnMut(&mut Rig, usize)) -> f32 {
    let mut previous: Vec<f32> = Vec::new();
    let mut largest = 0f32;
    for f in 0..2400 {
        run(r, f);
        r.update(FRAME);
        r.build().unwrap();
        if previous.len() == r.positions.len() {
            for (p, q) in r.positions.chunks(2).zip(previous.chunks(2)) {
                largest = largest.max(((p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2)).sqrt());
            }
        }
        previous.clone_from(&r.positions);
    }
    largest
}

#[test]
fn changing_expression_intensity_or_clip_never_jumps() {
    // A scripted session: expressions at their strongest, intensity changes in an idle, a long
    // idle, an interruption mid-transition, and the exit fade.
    let script = |r: &mut Rig, f: usize, shaped: bool| {
        let ids = idles(r);
        let play = |r: &mut Rig, state: usize, from: usize| {
            let name = format!("trans_{}_to_{}", STATES[from], STATES[state]);
            let t = r.animation_index(&name).unwrap_or_else(|| index(r, &format!("to_{}", STATES[state])));
            r.set_animation(0, t, false, 0.3);
            r.add_animation(0, ids[state], true, 0.2);
        };
        let set = |r: &mut Rig, state: Option<usize>, p: f64, m: f64| {
            if shaped {
                assert!(r.set_shape(state.map(|s| ids[s]), p, m));
            }
        };
        match f {
            0 => {
                r.set_animation(0, ids[2], true, 0.0);
                set(r, Some(2), 1.0, 1.0);
            }
            60 => {
                play(r, 0, 2);
                set(r, Some(0), 1.5, 1.7);
            }
            400 => set(r, Some(0), 0.65, 0.35), // intensity 10 -> 2 in the idle
            700 => {
                play(r, 6, 0);
                set(r, Some(6), 1.4, 1.4);
            }
            730 => {
                play(r, 5, 6); // interrupts the transition into sad
                set(r, Some(5), 1.3, 1.35);
            }
            1000 => {
                let long = index(r, "long_idle_angry");
                r.set_animation(0, long, false, 0.3);
                r.add_animation(0, ids[5], true, 0.4);
            }
            1500 => {
                play(r, 4, 5);
                set(r, Some(4), 1.25, 1.7);
            }
            1800 => {
                play(r, 3, 4);
                set(r, Some(3), 1.5, 1.4);
            }
            2100 => {
                let exit = index(r, "exit");
                r.set_animation(1, exit, false, 0.0);
                set(r, None, 1.0, 1.0);
            }
            _ => {}
        }
    };
    let canonical = largest_step(&mut rig(), &mut |r, f| script(r, f, false));
    let mut shaped = configured();
    let layered = largest_step(&mut shaped, &mut |r, f| script(r, f, true));
    // The layers add at most a fraction of a unit to the largest authored frame-to-frame motion.
    assert!(layered <= canonical * 1.05 + 0.25, "with layers {layered} vs canonical {canonical}");
    assert!(!shaped.shape().unwrap().active(), "the exit faded every gain back to 1");
}

#[test]
fn gains_ramp_smoothly_to_their_target_and_back() {
    let mut r = configured();
    let (happy, sad) = (idle(&r, "happy"), idle(&r, "sad"));
    assert!(r.set_shape(Some(happy), 1.5, 1.7));
    r.update(FRAME);
    let [p1, m1] = r.shape().unwrap().gains(happy).unwrap();
    assert!(p1 > 1.0 && p1 < 1.01, "eases in: {p1}");
    assert!(m1 > 1.0 && m1 < 1.03, "eases in: {m1}");
    let mut previous = p1;
    for _ in 0..((POSTURE_RAMP / FRAME) as usize + 2) {
        r.update(FRAME);
        let [p, _] = r.shape().unwrap().gains(happy).unwrap();
        assert!(p >= previous && p - previous < 0.05, "monotone, no jump: {previous} -> {p}");
        previous = p;
    }
    assert_eq!(r.shape().unwrap().gains(happy).unwrap(), [1.5, 1.7], "exactly the target once done");
    // The same target again restarts nothing.
    assert!(r.set_shape(Some(happy), 1.5, 1.7));
    r.update(FRAME);
    assert_eq!(r.shape().unwrap().gains(happy).unwrap(), [1.5, 1.7]);
    // Another expression: it ramps in while the previous one ramps out, continuously.
    assert!(r.set_shape(Some(sad), 1.4, 1.4));
    r.update(FRAME);
    let [hp, _] = r.shape().unwrap().gains(happy).unwrap();
    let [sp, _] = r.shape().unwrap().gains(sad).unwrap();
    assert!(hp < 1.5 && hp > 1.49, "{hp}");
    assert!(sp > 1.0 && sp < 1.01, "{sp}");
    // Retargeting mid-ramp continues from where it is.
    r.update(0.2);
    let [before, _] = r.shape().unwrap().gains(sad).unwrap();
    assert!(r.set_shape(Some(sad), 0.6, 0.25));
    r.update(FRAME);
    let [after, _] = r.shape().unwrap().gains(sad).unwrap();
    assert!((after - before).abs() < 0.01, "{before} -> {after}");
    // The exit: everything back to exactly 1, then inactive.
    assert!(r.set_shape(None, 1.0, 1.0));
    for _ in 0..((POSTURE_RAMP + 0.1) / FRAME) as usize {
        r.update(FRAME);
    }
    for a in idles(&r) {
        assert_eq!(r.shape().unwrap().gains(a).unwrap(), [1.0, 1.0]);
    }
    assert!(!r.shape().unwrap().active());
}

#[test]
fn the_guard_limits_only_what_was_added() {
    let mut plain = rig();
    let mut shaped = rig();
    let (i, rg) = (idles(&shaped), range(&shaped));
    let all = vec![1.0f32; i.len() * shaped.bone_count()];
    let sad = idle(&shaped, "sad");
    shaped.configure_shape(idle(&shaped, "default"), &i, &rg, &all, &all, POSTURE_RAMP, MOTION_RAMP).unwrap();
    both(&mut [&mut plain, &mut shaped], |r| {
        r.set_animation(0, sad, true, 0.0);
    });
    // Far beyond any profile: the guard must engage, and only ever between the authored range
    // and the canonical value.
    shaped.set_shape(Some(sad), MAX_GAIN, MAX_GAIN);
    for _ in 0..400 {
        frames(&mut [&mut plain, &mut shaped], FRAME);
        for b in 0..plain.bone_count() {
            let (low, high) = shaped.shape().unwrap().range(b).unwrap();
            let (p, s) = (plain.local(b).unwrap(), shaped.local(b).unwrap());
            for c in 0..CHANNELS {
                assert!(s[c] >= low[c].min(p[c]) - 1e-9 && s[c] <= high[c].max(p[c]) + 1e-9, "bone {b} channel {c}");
            }
        }
    }
    let stats = shaped.shape().unwrap().stats();
    assert!(stats.clamps > 0 && stats.largest > 0.0, "{stats:?}");
    shaped.shape_mut().unwrap().reset_stats();
    assert_eq!(shaped.shape().unwrap().stats().clamps, 0);
}

#[test]
fn steady_playback_with_layers_does_not_grow_its_buffers() {
    let mut r = configured();
    let happy = idle(&r, "happy");
    r.set_animation(0, happy, true, 0.0);
    r.set_shape(Some(happy), 1.5, 1.7);
    r.update(FRAME);
    r.build().unwrap();
    let caps = (r.positions.capacity(), r.texs.capacity(), r.indices.capacity());
    for _ in 0..600 {
        r.update(FRAME);
        r.build().unwrap();
    }
    assert_eq!(caps, (r.positions.capacity(), r.texs.capacity(), r.indices.capacity()));
}

#[test]
fn bad_configurations_and_targets_are_refused() {
    let mut r = rig();
    let happy = idle(&r, "happy");
    assert!(!r.set_shape(Some(happy), 1.2, 1.2), "nothing to set before configuring");
    let (i, rg) = (idles(&r), range(&r));
    let n = i.len() * r.bone_count();
    let ok = vec![1.0f32; n];
    let reference = idle(&r, "default");
    let enter = index(&r, "enter");
    assert!(r.configure_shape(reference, &[], &rg, &[], &[], 0.8, 0.5).is_err());
    assert!(r.configure_shape(reference, &[happy, happy], &rg, &vec![1.0; 2 * r.bone_count()], &vec![1.0; 2 * r.bone_count()], 0.8, 0.5).is_err());
    assert!(r.configure_shape(enter, &i, &rg, &ok, &ok, 0.8, 0.5).is_err(), "the reference must be an idle");
    assert!(r.configure_shape(reference, &i, &[enter], &ok, &ok, 0.8, 0.5).is_err(), "the range must cover the idles");
    assert!(r.configure_shape(reference, &[999], &rg, &ok[..r.bone_count()], &ok[..r.bone_count()], 0.8, 0.5).is_err());
    assert!(r.configure_shape(reference, &i, &rg, &ok[1..], &ok, 0.8, 0.5).is_err());
    let mut nan = ok.clone();
    nan[3] = f32::NAN;
    assert!(r.configure_shape(reference, &i, &rg, &nan, &ok, 0.8, 0.5).is_err());
    let mut big = ok.clone();
    big[0] = 1.5;
    assert!(r.configure_shape(reference, &i, &rg, &ok, &big, 0.8, 0.5).is_err());
    assert!(r.configure_shape(reference, &i, &rg, &ok, &ok, 0.0, 0.5).is_err());
    assert!(r.configure_shape(reference, &i, &rg, &ok, &ok, 0.8, f64::NAN).is_err());
    assert!(r.shape().is_none(), "a refused configuration leaves none");
    r.configure_shape(reference, &i, &rg, &ok, &ok, 0.8, 0.5).unwrap();
    for (p, m) in [(f64::NAN, 1.0), (1.0, f64::INFINITY), (-0.1, 1.0), (1.0, MAX_GAIN + 0.1)] {
        assert!(!r.set_shape(Some(happy), p, m), "{p} {m}");
    }
    assert!(!r.set_shape(Some(enter), 1.2, 1.2), "not a registered idle");
    assert!(!r.shape().unwrap().active(), "refused targets change nothing");
}
