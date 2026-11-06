//! A frame with the presentation-intensity layers active allocates nothing more than a canonical
//! frame. Its own test binary, so the counting allocator sees only this test; allocations are
//! counted per thread, so the harness's own threads never interfere.

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

use jarvis_rig::rig::Rig;

struct Counting;

thread_local! {
    static ALLOCATIONS: Cell<u64> = const { Cell::new(0) };
}

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let _ = ALLOCATIONS.try_with(|n| n.set(n.get() + 1));
        System.alloc(layout)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout)
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let _ = ALLOCATIONS.try_with(|n| n.set(n.get() + 1));
        System.realloc(ptr, layout, new_size)
    }
}

#[global_allocator]
static GLOBAL: Counting = Counting;

const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../assets/character/rig");
const STATES: [&str; 7] = ["happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad"];
const FRAME: f64 = 1.0 / 60.0;

fn rig(shaped: bool) -> Rig {
    let skel = std::fs::read(format!("{DIR}/JarvisCharacter.skel")).unwrap();
    let atlas = std::fs::read_to_string(format!("{DIR}/JarvisCharacter.atlas")).unwrap();
    let mut r = Rig::load(&skel, &atlas).unwrap();
    if shaped {
        let idles: Vec<usize> = STATES.iter().map(|s| r.animation_index(&format!("idle_{s}")).unwrap()).collect();
        let overlay = [r.animation_index("enter").unwrap(), r.animation_index("exit").unwrap()];
        let range: Vec<usize> = (0..r.animation_count()).filter(|i| !overlay.contains(i)).collect();
        let w = vec![1.0f32; idles.len() * r.bone_count()];
        r.configure_shape(idles[2], &idles, &range, &w, &w, 0.8, 0.5).unwrap();
    }
    r
}

/// Allocations during a fixed session (an idle, its long idle, a transition), after warm-up.
fn allocations(shaped: bool) -> u64 {
    let mut r = rig(shaped);
    let (happy, long, to_sad, sad) = (
        r.animation_index("idle_happy").unwrap(),
        r.animation_index("long_idle_happy").unwrap(),
        r.animation_index("to_sad").unwrap(),
        r.animation_index("idle_sad").unwrap(),
    );
    r.set_animation(0, happy, true, 0.0);
    if shaped {
        r.set_shape(Some(happy), 1.5, 1.7);
    }
    for _ in 0..120 {
        r.update(FRAME);
        r.build().unwrap();
    }
    let start = ALLOCATIONS.with(Cell::get);
    for f in 0..900 {
        if f == 100 {
            r.set_animation(0, long, false, 0.3);
            r.add_animation(0, happy, true, 0.4);
        }
        if f == 500 {
            r.set_animation(0, to_sad, false, 0.3);
            r.add_animation(0, sad, true, 0.2);
            if shaped {
                r.set_shape(Some(sad), 1.4, 1.4);
            }
        }
        r.update(FRAME);
        r.build().unwrap();
        r.take_events(64);
    }
    ALLOCATIONS.with(Cell::get) - start
}

#[test]
fn the_layers_allocate_nothing_per_frame() {
    let canonical = allocations(false);
    let shaped = allocations(true);
    println!("allocations over the session: canonical {canonical}, with layers {shaped}");
    assert_eq!(shaped, canonical, "the intensity layers allocated on their own (canonical session: {canonical})");
}
