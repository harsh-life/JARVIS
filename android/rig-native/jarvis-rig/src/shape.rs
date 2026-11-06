//! Presentation intensity on the native side: two additive layers over the canonical pose.
//!
//! The animation state poses the skeleton exactly as authored. When the player asks for an
//! expression's intensity, this adds, per bone and channel of the local transforms:
//!
//! - **Posture emphasis:** `(posture gain - 1) * (held pose - reference pose)`, where an idle's
//!   *held pose* is its time average over one loop (sampled with the runtime's own sampler) and the
//!   *reference* is the reference idle's (Default's) held pose. It is applied whatever plays
//!   (entry, transition, idle, long idle), so changing expression retargets it smoothly instead
//!   of jumping when a clip ends. The clips stay canonical; the rendered pose during a transition
//!   is not strictly canonical when a gain is not 1.
//! - **Idle motion:** `(motion gain - 1) * (idle pose at its time - held pose)`, only for the
//!   registered idles on track 0, weighted by how much of the pose each one is (its crossfade
//!   weight), so an idle fading in or out scales with it. Long idles, entries, transitions, the
//!   entrance and the exit are never scaled.
//!
//! Every gain ramps (smoothstep) to its target; a registered idle that is not the shown one ramps
//! to 1. With every gain at exactly 1 nothing is touched, so the output is bit-identical to the
//! canonical one. A per-bone weight of 0 keeps that bone canonical in that layer.
//!
//! The **authored-range guard** keeps every adjusted channel inside the range the rig's own
//! animations reach for it (sampled at load) — but never tighter than the canonical value of that
//! frame, so it only ever limits what was added. Clamps are counted, so tests can tell a guard
//! that limits a rare peak from one that flattens normal behaviour.
//!
//! It knows nothing about expressions or why a gain is what it is: the Kotlin player resolves an
//! (expression, intensity) pair to gains through the intensity profile. All reference data is
//! computed in [`Shape::configure`]; a frame allocates nothing.

use rigby_runtime::math::Transform;
use rigby_runtime::state::TrackEntry;
use rigby_runtime::{AnimationState, Pose, SkeletonData};

/// Local channels: x, y, rotation, scale x, scale y, shear x, shear y.
pub const CHANNELS: usize = 7;
type Channels = [f64; CHANNELS];

/// Held poses are averaged over this many samples a second of the idle's loop.
const HELD_RATE: f64 = 120.0;
/// The authored range is sampled this many times a second of each animation.
const RANGE_RATE: f64 = 60.0;
/// The largest gain the native side accepts (the profile's own limits are tighter).
pub const MAX_GAIN: f64 = 3.0;
/// The longest ramp, in seconds.
pub const MAX_RAMP: f64 = 10.0;
/// A layer contribution smaller than this (units, degrees or scale) is rounding noise, not a change.
const NEGLIGIBLE: f64 = 1e-9;

fn channels(t: &Transform) -> Channels {
    [t.x, t.y, t.rotation, t.scale_x, t.scale_y, t.shear_x, t.shear_y]
}

fn set_channel(t: &mut Transform, c: usize, v: f64) {
    match c {
        0 => t.x = v,
        1 => t.y = v,
        2 => t.rotation = v,
        3 => t.scale_x = v,
        4 => t.scale_y = v,
        5 => t.shear_x = v,
        _ => t.shear_y = v,
    }
}

/// A gain easing (smoothstep) from where it was to its target over `seconds`; exactly the target
/// once done.
#[derive(Clone, Copy, Debug)]
struct Ramp {
    from: f64,
    to: f64,
    value: f64,
    elapsed: f64,
    seconds: f64,
}

impl Ramp {
    fn new(seconds: f64) -> Ramp {
        Ramp { from: 1.0, to: 1.0, value: 1.0, elapsed: seconds, seconds }
    }

    fn retarget(&mut self, to: f64) {
        if to != self.to {
            self.from = self.value;
            self.to = to;
            self.elapsed = 0.0;
        }
    }

    fn advance(&mut self, dt: f64) {
        if self.value == self.to {
            return;
        }
        self.elapsed += dt;
        let u = (self.elapsed / self.seconds).min(1.0);
        self.value = if u >= 1.0 { self.to } else { self.from + (self.to - self.from) * u * u * (3.0 - 2.0 * u) };
    }

    fn neutral(&self) -> bool {
        self.value == 1.0 && self.to == 1.0
    }
}

struct Idle {
    animation: usize,
    duration: f64,
    held: Vec<Channels>,
    posture_weight: Vec<f64>,
    motion_weight: Vec<f64>,
    posture: Ramp,
    motion: Ramp,
}

/// Statistics of the guard since configuration (or the last [`Shape::reset_stats`]).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct GuardStats {
    /// Channel values the guard changed.
    pub clamps: u64,
    /// The largest change, and the bone and channel it was in.
    pub largest: f64,
    pub largest_bone: usize,
    pub largest_channel: usize,
}

pub struct Shape {
    reference: Vec<Channels>,
    idles: Vec<Idle>,
    low: Vec<Channels>,
    high: Vec<Channels>,
    scratch: Pose,
    delta: Vec<Channels>,
    stats: GuardStats,
}

impl Shape {
    /// Prepares the layers for `data`.
    ///
    /// `idles` are the regular idle animations (one per expression); `reference` is one of them
    /// (Default's). `range` are the animations whose reach defines the guard (every track-0
    /// animation of the contract; it must include the idles). `posture_weights` and
    /// `motion_weights` hold one weight in 0..=1 per bone for each idle, in `idles` order.
    #[allow(clippy::too_many_arguments)]
    pub fn configure(
        data: &SkeletonData,
        reference: usize,
        idles: &[usize],
        range: &[usize],
        posture_weights: &[f32],
        motion_weights: &[f32],
        posture_ramp: f64,
        motion_ramp: f64,
    ) -> Result<Shape, String> {
        let bones = data.bones.len();
        let animations = data.animations.len();
        if idles.is_empty() || idles.iter().any(|&a| a >= animations) {
            return Err("shape: an idle is not an animation of the rig".into());
        }
        if (1..idles.len()).any(|i| idles[..i].contains(&idles[i])) {
            return Err("shape: an idle is named twice".into());
        }
        if !idles.contains(&reference) {
            return Err("shape: the reference is not one of the idles".into());
        }
        if range.is_empty() || range.iter().any(|&a| a >= animations) || !idles.iter().all(|a| range.contains(a)) {
            return Err("shape: the range animations are invalid or omit an idle".into());
        }
        for w in [posture_weights, motion_weights] {
            if w.len() != idles.len() * bones || !w.iter().all(|v| v.is_finite() && (0.0..=1.0).contains(v)) {
                return Err("shape: weights must be one value in 0..=1 per bone and idle".into());
            }
        }
        for r in [posture_ramp, motion_ramp] {
            if !(r.is_finite() && r > 0.0 && r <= MAX_RAMP) {
                return Err("shape: a ramp is out of range".into());
            }
        }
        let mut scratch = Pose::new(data);
        let mut held_of = |anim: usize| -> Vec<Channels> {
            let duration = data.animations[anim].duration;
            let n = ((duration * HELD_RATE).ceil() as usize).max(1);
            let mut sum = vec![[0.0; CHANNELS]; bones];
            let mut first = vec![[0.0; CHANNELS]; bones];
            let mut constant = vec![[true; CHANNELS]; bones];
            for k in 0..n {
                sample(data, &mut scratch, anim, duration * k as f64 / n as f64);
                for b in 0..bones {
                    let v = channels(&scratch.local[b]);
                    for c in 0..CHANNELS {
                        sum[b][c] += v[c];
                        if k == 0 {
                            first[b][c] = v[c];
                        } else if v[c] != first[b][c] {
                            constant[b][c] = false;
                        }
                    }
                }
            }
            // A channel the idle holds still is exactly that value (no averaging noise).
            (0..bones)
                .map(|b| std::array::from_fn(|c| if constant[b][c] { first[b][c] } else { sum[b][c] / n as f64 }))
                .collect()
        };
        let idle_list: Vec<Idle> = idles
            .iter()
            .enumerate()
            .map(|(i, &anim)| Idle {
                animation: anim,
                duration: data.animations[anim].duration,
                held: held_of(anim),
                posture_weight: posture_weights[i * bones..(i + 1) * bones].iter().map(|&w| w as f64).collect(),
                motion_weight: motion_weights[i * bones..(i + 1) * bones].iter().map(|&w| w as f64).collect(),
                posture: Ramp::new(posture_ramp),
                motion: Ramp::new(motion_ramp),
            })
            .collect();
        let reference = idle_list.iter().find(|i| i.animation == reference).map(|i| i.held.clone()).unwrap_or_default();
        let mut low = vec![[f64::INFINITY; CHANNELS]; bones];
        let mut high = vec![[f64::NEG_INFINITY; CHANNELS]; bones];
        for &anim in range {
            let duration = data.animations[anim].duration;
            let n = ((duration * RANGE_RATE).ceil() as usize).max(1);
            for k in 0..=n {
                sample(data, &mut scratch, anim, duration * k as f64 / n as f64);
                for b in 0..bones {
                    let v = channels(&scratch.local[b]);
                    for c in 0..CHANNELS {
                        low[b][c] = low[b][c].min(v[c]);
                        high[b][c] = high[b][c].max(v[c]);
                    }
                }
            }
        }
        Ok(Shape { reference, idles: idle_list, low, high, scratch, delta: vec![[0.0; CHANNELS]; bones], stats: GuardStats::default() })
    }

    /// Shows `idle`'s expression at these gains (ramping); every other idle ramps to 1. `None`
    /// ramps everything to 1 (the exit). False, changing nothing, for an unregistered idle or a
    /// gain that is not finite or outside 0..=[`MAX_GAIN`].
    pub fn set_target(&mut self, idle: Option<usize>, posture: f64, motion: f64) -> bool {
        let ok = |g: f64| g.is_finite() && (0.0..=MAX_GAIN).contains(&g);
        if !ok(posture) || !ok(motion) {
            return false;
        }
        if let Some(a) = idle {
            if !self.idles.iter().any(|i| i.animation == a) {
                return false;
            }
        }
        for i in &mut self.idles {
            let shown = Some(i.animation) == idle;
            i.posture.retarget(if shown { posture } else { 1.0 });
            i.motion.retarget(if shown { motion } else { 1.0 });
        }
        true
    }

    /// True while some gain is not exactly 1 or still ramping.
    pub fn active(&self) -> bool {
        self.idles.iter().any(|i| !i.posture.neutral() || !i.motion.neutral())
    }

    /// The current (ramped) gains of the registered idle `animation`: `[posture, motion]`.
    pub fn gains(&self, animation: usize) -> Option<[f64; 2]> {
        self.idles.iter().find(|i| i.animation == animation).map(|i| [i.posture.value, i.motion.value])
    }

    /// The held pose of the registered idle `animation`, per bone.
    pub fn held(&self, animation: usize) -> Option<&[Channels]> {
        self.idles.iter().find(|i| i.animation == animation).map(|i| i.held.as_slice())
    }

    pub fn reference(&self) -> &[Channels] {
        &self.reference
    }

    /// The authored range of one bone's channels: `(low, high)`.
    pub fn range(&self, bone: usize) -> Option<(Channels, Channels)> {
        Some((*self.low.get(bone)?, *self.high.get(bone)?))
    }

    pub fn stats(&self) -> GuardStats {
        self.stats
    }

    pub fn reset_stats(&mut self) {
        self.stats = GuardStats::default();
    }

    /// Advances the ramps by `dt` and, unless every gain is exactly 1, adds both layers to the
    /// canonical pose `state` just applied and recomputes the world transforms. Returns whether it
    /// changed the pose.
    pub fn apply(&mut self, data: &SkeletonData, state: &AnimationState, pose: &mut Pose, dt: f64) -> bool {
        for i in &mut self.idles {
            i.posture.advance(dt);
            i.motion.advance(dt);
        }
        if !self.active() {
            return false;
        }
        self.delta.fill([0.0; CHANNELS]);
        // Posture: every idle whose gain is not 1 (the shown one, and one still ramping out).
        for idle in &self.idles {
            let k = idle.posture.value - 1.0;
            if k == 0.0 {
                continue;
            }
            for (b, d) in self.delta.iter_mut().enumerate() {
                let w = idle.posture_weight[b];
                if w == 0.0 {
                    continue;
                }
                for ((d, held), reference) in d.iter_mut().zip(&idle.held[b]).zip(&self.reference[b]) {
                    *d += k * w * (held - reference);
                }
            }
        }
        // Motion: the registered idles on track 0, by their share of the pose.
        if let Some(entry) = state.current(0) {
            match entry.previous.as_deref() {
                Some(previous) => {
                    let a = entry.mix() * entry.alpha;
                    self.add_motion(data, entry, a);
                    self.add_motion(data, previous, (1.0 - a) * previous.alpha);
                }
                None => self.add_motion(data, entry, entry.alpha),
            }
        }
        let mut changed = false;
        for (b, d) in self.delta.iter().enumerate() {
            let Some(local) = pose.local.get_mut(b) else { break };
            let canonical = channels(local);
            for c in 0..CHANNELS {
                if d[c].abs() < NEGLIGIBLE {
                    continue;
                }
                let target = canonical[c] + d[c];
                // Never tighter than the canonical value: the guard limits only what was added.
                let low = self.low[b][c].min(canonical[c]);
                let high = self.high[b][c].max(canonical[c]);
                let value = target.clamp(low, high);
                if (target - value).abs() > NEGLIGIBLE {
                    self.stats.clamps += 1;
                    let amount = (target - value).abs();
                    if amount > self.stats.largest {
                        self.stats = GuardStats { clamps: self.stats.clamps, largest: amount, largest_bone: b, largest_channel: c };
                    }
                }
                set_channel(local, c, value);
                changed = true;
            }
        }
        if changed {
            pose.update_world(data);
        }
        changed
    }

    fn add_motion(&mut self, data: &SkeletonData, entry: &TrackEntry, weight: f64) {
        if weight <= 0.0 {
            return;
        }
        let Some(idle) = self.idles.iter().find(|i| i.animation == entry.animation) else { return };
        let k = idle.motion.value - 1.0;
        if k == 0.0 {
            return;
        }
        // The same time arithmetic as the animation state's own apply.
        let d = idle.duration;
        let time = if entry.looping && d > 0.0 { ((entry.time % d) + d) % d } else { entry.time.min(d) };
        sample(data, &mut self.scratch, idle.animation, time);
        for (b, delta) in self.delta.iter_mut().enumerate() {
            let w = idle.motion_weight[b];
            if w == 0.0 {
                continue;
            }
            let v = channels(&self.scratch.local[b]);
            for ((d, v), held) in delta.iter_mut().zip(v).zip(&idle.held[b]) {
                *d += weight * k * w * (v - held);
            }
        }
    }
}

/// Poses `anim` alone at `time` (from the setup pose) into `pose`'s local transforms.
fn sample(data: &SkeletonData, pose: &mut Pose, anim: usize, time: f64) {
    pose.reset(data);
    pose.apply(data, anim, time, 1.0);
}
