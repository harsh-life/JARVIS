//! The native core against the character the app bundles (assets/character/rig/).

use jarvis_rig::rig::{Rig, EVENT_COMPLETE};

const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../assets/character/rig");
const STATES: [&str; 7] = ["happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad"];
const PAIRS: [&str; 5] = ["trans_happy_to_satisfied", "trans_surprised_to_default", "trans_angry_to_satisfied", "trans_sad_to_happy", "trans_sleepy_to_default"];
const FRAME: f64 = 1.0 / 60.0;

fn bundled() -> (Vec<u8>, String) {
    let skel = std::fs::read(format!("{DIR}/JarvisCharacter.skel")).expect("bundled skeleton");
    let atlas = std::fs::read_to_string(format!("{DIR}/JarvisCharacter.atlas")).expect("bundled atlas");
    (skel, atlas)
}

fn rig() -> Rig {
    let (skel, atlas) = bundled();
    Rig::load(&skel, &atlas).expect("the bundled rig loads")
}

fn index(rig: &Rig, name: &str) -> usize {
    rig.animation_index(name).unwrap_or_else(|| panic!("{name} missing"))
}

fn run(rig: &mut Rig, seconds: f64) {
    for _ in 0..(seconds / FRAME).round() as usize {
        rig.update(FRAME);
    }
}

#[test]
fn every_contract_animation_exists() {
    let rig = rig();
    let mut names: Vec<String> = Vec::new();
    for s in STATES {
        names.extend([format!("idle_{s}"), format!("long_idle_{s}"), format!("to_{s}")]);
    }
    names.extend(PAIRS.iter().map(|s| s.to_string()));
    names.extend(["enter".into(), "exit".into()]);
    assert_eq!(names.len(), 28);
    for n in &names {
        let i = index(&rig, n);
        assert!(rig.animation_duration(i).unwrap() > 0.0, "{n}");
    }
    assert_eq!(rig.animation_count(), 28);
}

#[test]
fn the_setup_frame_matches_the_artwork() {
    let rig = rig();
    let [x0, y0, x1, y1] = rig.bounds();
    // The character is drawn about 210 x 300 units (default.png pixels) with its feet at 0.
    assert!((200.0..240.0).contains(&(x1 - x0)), "{x0} {x1}");
    assert!((290.0..320.0).contains(&(y1 - y0)), "{y0} {y1}");
    assert!(y0.abs() < 5.0);
    assert_eq!(rig.page_size(), [512.0, 512.0]);
}

#[test]
fn an_entry_hands_over_to_its_idle() {
    let mut rig = rig();
    let to_happy = index(&rig, "to_happy");
    let idle_happy = index(&rig, "idle_happy");
    assert!(rig.set_animation(0, to_happy, false, 0.3));
    assert!(rig.add_animation(0, idle_happy, true, 0.2));
    assert_eq!(rig.current(0), Some(to_happy));
    assert_eq!(rig.queued(0), 1);
    let d = rig.animation_duration(to_happy).unwrap();
    run(&mut rig, d + 0.1);
    assert_eq!(rig.current(0), Some(idle_happy));
    assert_eq!(rig.queued(0), 0);
    // A loop never ends by itself.
    run(&mut rig, 10.0);
    assert_eq!(rig.current(0), Some(idle_happy));
}

#[test]
fn a_new_state_interrupts_a_transition_and_drops_its_queue() {
    let mut rig = rig();
    let (to_sad, idle_sad, to_angry) = (index(&rig, "to_sad"), index(&rig, "idle_sad"), index(&rig, "to_angry"));
    rig.set_animation(0, to_sad, false, 0.3);
    rig.add_animation(0, idle_sad, true, 0.2);
    run(&mut rig, 0.2);
    rig.set_animation(0, to_angry, false, 0.15);
    assert_eq!(rig.current(0), Some(to_angry));
    assert_eq!(rig.queued(0), 0, "the old idle must not play after the new transition");
}

#[test]
fn a_long_idle_plays_once_then_its_idle_returns() {
    let mut rig = rig();
    let (long, idle) = (index(&rig, "long_idle_default"), index(&rig, "idle_default"));
    rig.set_animation(0, long, false, 0.3);
    rig.add_animation(0, idle, true, 0.4);
    rig.take_events(64);
    let duration = rig.animation_duration(long).unwrap();
    let mut completed = 0;
    for _ in 0..((duration + 1.0) / FRAME) as usize {
        rig.update(FRAME);
        let events = rig.take_events(64).to_vec();
        completed += events.chunks(3).filter(|e| e[0] == EVENT_COMPLETE && e[2] == long as i32).count();
    }
    assert_eq!(completed, 1);
    assert_eq!(rig.current(0), Some(idle));
}

#[test]
fn the_entrance_completes_on_its_own_track() {
    let mut rig = rig();
    let (idle, enter) = (index(&rig, "idle_default"), index(&rig, "enter"));
    rig.set_animation(0, idle, true, 0.0);
    rig.set_animation(1, enter, false, 0.0);
    rig.take_events(64);
    let mut seen = false;
    for _ in 0..90 {
        rig.update(FRAME);
        seen |= rig.take_events(64).chunks(3).any(|e| e == [EVENT_COMPLETE, 1, enter as i32]);
    }
    assert!(seen);
    rig.clear_track(1);
    assert_eq!(rig.current(1), None);
    assert_eq!(rig.current(0), Some(idle));
}

#[test]
fn geometry_stays_within_capacity_in_every_animation() {
    let mut rig = rig();
    for i in 0..rig.animation_count() {
        rig.set_animation(0, i, true, 0.0);
        for _ in 0..(rig.animation_duration(i).unwrap() / 0.1).ceil() as usize {
            rig.update(0.1);
            rig.build().unwrap();
            let vertices = rig.positions.len() / 2;
            assert!(vertices <= rig.vertex_capacity());
            assert!(rig.indices.len() <= rig.index_capacity());
            assert_eq!(rig.texs.len(), rig.positions.len());
            assert!(rig.indices.iter().all(|&k| (k as usize) < vertices));
            assert!(rig.texs.iter().all(|t| (0.0..=512.0).contains(t)));
            assert!(rig.positions.iter().all(|p| p.is_finite()));
        }
    }
}

#[test]
fn steady_playback_does_not_grow_its_buffers() {
    let mut rig = rig();
    rig.set_animation(0, index(&rig, "idle_happy"), true, 0.0);
    rig.update(FRAME);
    rig.build().unwrap();
    let caps = (rig.positions.capacity(), rig.texs.capacity(), rig.indices.capacity());
    for _ in 0..600 {
        rig.update(FRAME);
        rig.build().unwrap();
    }
    assert_eq!(caps, (rig.positions.capacity(), rig.texs.capacity(), rig.indices.capacity()));
}

#[test]
fn time_never_jumps_or_breaks() {
    let mut rig = rig();
    let idle = index(&rig, "idle_default");
    rig.set_animation(0, idle, true, 0.0);
    rig.update(f64::NAN);
    rig.update(-1.0);
    assert_eq!(rig.current_time(0), 0.0);
    rig.update(30.0); // a stalled frame advances at most 0.25 s
    assert!((rig.current_time(0) - 0.25).abs() < 1e-9);
}

#[test]
fn unknown_animations_and_tracks_are_refused() {
    let mut rig = rig();
    assert_eq!(rig.animation_index("idle_bored"), None);
    assert!(!rig.set_animation(0, 999, true, 0.0));
    assert!(!rig.add_animation(0, 999, true, 0.0));
    assert!(!rig.set_animation(99, 0, true, 0.0));
    rig.clear_track(99);
}

#[test]
fn malformed_input_is_an_error_not_a_crash() {
    let (skel, atlas) = bundled();
    assert!(Rig::load(b"", &atlas).is_err());
    assert!(Rig::load(b"not a skeleton", &atlas).is_err());
    assert!(Rig::load(&skel[..skel.len() / 2], &atlas).is_err());
    assert!(Rig::load(&skel, "").is_err());
    assert!(Rig::load(&skel, "garbage\nmore garbage").is_err());
}

#[test]
fn an_atlas_missing_a_region_is_refused() {
    let (skel, atlas) = bundled();
    let mut lines: Vec<&str> = atlas.lines().collect();
    let at = lines.iter().position(|l| *l == "eyes_closed").expect("region in the atlas");
    lines.drain(at..at + 2);
    let err = Rig::load(&skel, &lines.join("\n")).err().expect("refused");
    assert!(err.contains("eyes_closed"), "{err}");
}

#[test]
fn an_atlas_with_two_pages_is_refused() {
    let (skel, atlas) = bundled();
    let two = format!("{atlas}\nsecond.png\nsize: 16, 16\nfilter: Linear, Linear\nextra\nbounds: 0, 0, 4, 4\n");
    assert!(Rig::load(&skel, &two).is_err());
}
