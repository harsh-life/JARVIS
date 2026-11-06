//! The character player's native core, free of JNI so `cargo test` exercises it directly.
//!
//! A [`Rig`] owns one loaded skeleton, its atlas, an animation state, a pose and the geometry of
//! the last frame. It only plays and draws what it is told to: it has no notion of what an
//! expression means, and no timer of its own (time only advances in [`Rig::update`]).
//!
//! Geometry is flattened once per frame into buffers sized at load time for the worst case (every
//! slot showing its largest attachment), so playback never allocates after the first frame.
//!
//! An optional [`Shape`] (presentation intensity) adds posture and idle-motion layers over the
//! canonical pose; until it is configured and given a gain other than 1, nothing changes.

use rigby_runtime::model::{AttachmentKind, Blend};
use rigby_runtime::{load_skeleton, AnimationState, Atlas, Pose, RegionLookup, RenderOutput, SkeletonData, StateEventKind};

use crate::shape::{Shape, CHANNELS};

/// A track callback, as plain numbers for the JNI side: `[kind, track, animation]`.
pub const EVENT_START: i32 = 0;
pub const EVENT_END: i32 = 1;
pub const EVENT_COMPLETE: i32 = 2;
/// Callbacks kept between two drains; older ones are dropped (the player drains every frame).
const MAX_EVENTS: usize = 64;
const MAX_TRACKS: usize = 4;

pub struct Rig {
    data: SkeletonData,
    lookup: RegionLookup,
    pose: Pose,
    state: AnimationState,
    out: RenderOutput,
    page_width: f32,
    page_height: f32,
    /// Flattened geometry of the last [`Rig::build`]: x, y pairs (world units, Y up); texture
    /// coordinates in page pixels; triangle indices.
    pub positions: Vec<f32>,
    pub texs: Vec<f32>,
    pub indices: Vec<u16>,
    vertex_capacity: usize,
    index_capacity: usize,
    events: Vec<[i32; 3]>,
    flat_events: Vec<i32>,
    bounds: [f32; 4],
    shape: Option<Shape>,
}

impl Rig {
    /// Loads a skeleton (binary `.skel` or JSON) and its atlas text. Refuses what this player does
    /// not draw (several atlas pages, clipping, linked meshes, blend modes other than normal,
    /// two-color tint) and any image missing from the atlas, so a caller can fall back cleanly
    /// instead of drawing a wrong character.
    pub fn load(skeleton: &[u8], atlas_text: &str) -> Result<Rig, String> {
        let data = load_skeleton(skeleton).map_err(|e| format!("skeleton: {}", e.0))?;
        let atlas = Atlas::parse(atlas_text);
        if atlas.pages.len() != 1 {
            return Err(format!("atlas: expected one page, found {}", atlas.pages.len()));
        }
        let page = &atlas.pages[0];
        if !(page.width > 0.0 && page.height > 0.0) {
            return Err("atlas: the page has no size".into());
        }
        let lookup = RegionLookup::new(&data, &atlas);
        let missing = lookup.missing(&data);
        if !missing.is_empty() {
            return Err(format!("atlas: no region for {}", missing.join(", ")));
        }
        let (vertex_capacity, index_capacity) = capacity(&data)?;
        if vertex_capacity > u16::MAX as usize {
            return Err("skeleton: too many vertices for 16-bit indices".into());
        }
        let mut rig = Rig {
            pose: Pose::new(&data),
            lookup,
            state: AnimationState::new(),
            out: RenderOutput::default(),
            page_width: page.width as f32,
            page_height: page.height as f32,
            positions: Vec::with_capacity(vertex_capacity * 2),
            texs: Vec::with_capacity(vertex_capacity * 2),
            indices: Vec::with_capacity(index_capacity),
            vertex_capacity,
            index_capacity,
            events: Vec::with_capacity(MAX_EVENTS),
            flat_events: Vec::with_capacity(MAX_EVENTS * 3),
            bounds: [0.0; 4],
            shape: None,
            data,
        };
        rig.pose.reset(&rig.data);
        rig.pose.update_world(&rig.data);
        rig.build()?;
        rig.bounds = rig.geometry_bounds();
        Ok(rig)
    }

    pub fn animation_count(&self) -> usize {
        self.data.animations.len()
    }

    pub fn animation_index(&self, name: &str) -> Option<usize> {
        self.data.find_animation(name)
    }

    pub fn animation_duration(&self, index: usize) -> Option<f64> {
        self.data.animations.get(index).map(|a| a.duration)
    }

    /// Plays animation `index` on `track` now, crossfading over `mix` seconds. False for an unknown
    /// animation or track.
    pub fn set_animation(&mut self, track: usize, index: usize, looping: bool, mix: f64) -> bool {
        let Some(name) = self.name_of(track, index) else { return false };
        self.state.set_animation(&self.data, track, &name, looping, mix.max(0.0)).is_some()
    }

    /// Queues animation `index` after what `track` plays (starts at once on an empty track).
    pub fn add_animation(&mut self, track: usize, index: usize, looping: bool, mix: f64) -> bool {
        let Some(name) = self.name_of(track, index) else { return false };
        self.state.add_animation(&self.data, track, &name, looping, mix.max(0.0), None).is_some()
    }

    pub fn clear_track(&mut self, track: usize) {
        if track < MAX_TRACKS {
            self.state.clear_track(track);
            self.collect_events();
        }
    }

    /// The animation `track` plays (not a queued one), or None when the track is empty.
    pub fn current(&self, track: usize) -> Option<usize> {
        self.state.current(track).map(|e| e.animation)
    }

    /// The current entry's time on `track` (seconds), or a negative number when it is empty.
    pub fn current_time(&self, track: usize) -> f64 {
        self.state.current(track).map_or(-1.0, |e| e.time)
    }

    /// How many entries wait behind the current one on `track`.
    pub fn queued(&self, track: usize) -> usize {
        self.state.current(track).map_or(0, |e| e.queue.len())
    }

    /// Advances playback by `dt` seconds (clamped to 0..0.25 so a stall never jumps ahead) and
    /// poses the skeleton: the canonical pose, then the intensity layers if a [`Shape`] is active.
    pub fn update(&mut self, dt: f64) {
        let dt = if dt.is_finite() { dt.clamp(0.0, 0.25) } else { 0.0 };
        self.state.update(&self.data, dt);
        self.state.apply(&self.data, &mut self.pose);
        if let Some(shape) = &mut self.shape {
            shape.apply(&self.data, &self.state, &mut self.pose, dt);
        }
        self.collect_events();
    }

    pub fn bone_count(&self) -> usize {
        self.data.bones.len()
    }

    /// The index of the bone named `name`, or None.
    pub fn bone_index(&self, name: &str) -> Option<usize> {
        self.data.find_bone(name)
    }

    /// Prepares the intensity layers (see [`Shape::configure`]): computes the idles' held poses,
    /// the reference and the authored range once, here. Replaces any previous configuration.
    #[allow(clippy::too_many_arguments)]
    pub fn configure_shape(
        &mut self,
        reference: usize,
        idles: &[usize],
        range: &[usize],
        posture_weights: &[f32],
        motion_weights: &[f32],
        posture_ramp: f64,
        motion_ramp: f64,
    ) -> Result<(), String> {
        let shape = Shape::configure(&self.data, reference, idles, range, posture_weights, motion_weights, posture_ramp, motion_ramp)?;
        self.shape = Some(shape);
        Ok(())
    }

    /// Ramps to `idle`'s gains (see [`Shape::set_target`]); false without a configured shape.
    pub fn set_shape(&mut self, idle: Option<usize>, posture: f64, motion: f64) -> bool {
        self.shape.as_mut().is_some_and(|s| s.set_target(idle, posture, motion))
    }

    pub fn shape(&self) -> Option<&Shape> {
        self.shape.as_ref()
    }

    pub fn shape_mut(&mut self) -> Option<&mut Shape> {
        self.shape.as_mut()
    }

    /// A bone's current local transform as `[x, y, rotation, scale x, scale y, shear x, shear y]`.
    pub fn local(&self, bone: usize) -> Option<[f64; CHANNELS]> {
        self.pose.local.get(bone).map(|t| [t.x, t.y, t.rotation, t.scale_x, t.scale_y, t.shear_x, t.shear_y])
    }

    /// Track callbacks since the last call, oldest first, flattened as `[kind, track, animation]`
    /// triples (at most `max` of them; the rest are dropped). The slice is reused between calls.
    pub fn take_events(&mut self, max: usize) -> &[i32] {
        self.flat_events.clear();
        for e in self.events.iter().take(max) {
            self.flat_events.extend_from_slice(e);
        }
        self.events.clear();
        &self.flat_events
    }

    /// Flattens the current pose into [`Rig::positions`], [`Rig::texs`] and [`Rig::indices`].
    pub fn build(&mut self) -> Result<(), String> {
        self.out.build(&self.data, &self.pose, &self.lookup, [1.0; 4]);
        self.positions.clear();
        self.texs.clear();
        self.indices.clear();
        for b in self.out.batches() {
            if b.page != 0 || b.blend != Blend::Normal {
                return Err("geometry: unsupported page or blend mode".into());
            }
            let base = self.positions.len() / 2;
            if base + b.positions.len() > self.vertex_capacity || self.indices.len() + b.indices.len() > self.index_capacity {
                return Err("geometry: over capacity".into());
            }
            for (p, uv) in b.positions.iter().zip(&b.uvs) {
                self.positions.extend_from_slice(&[p[0], p[1]]);
                self.texs.extend_from_slice(&[uv[0] * self.page_width, uv[1] * self.page_height]);
            }
            self.indices.extend(b.indices.iter().map(|&i| (base + i as usize) as u16));
        }
        Ok(())
    }

    pub fn vertex_capacity(&self) -> usize {
        self.vertex_capacity
    }

    pub fn index_capacity(&self) -> usize {
        self.index_capacity
    }

    /// The setup pose's bounds `[min x, min y, max x, max y]` (world units, Y up): the frame the
    /// view fits, so motion never rescales the character.
    pub fn bounds(&self) -> [f32; 4] {
        self.bounds
    }

    pub fn page_size(&self) -> [f32; 2] {
        [self.page_width, self.page_height]
    }

    fn name_of(&self, track: usize, index: usize) -> Option<String> {
        if track >= MAX_TRACKS {
            return None;
        }
        self.data.animations.get(index).map(|a| a.name.clone())
    }

    fn collect_events(&mut self) {
        for e in self.state.drain_events() {
            let kind = match e.kind {
                StateEventKind::Start => EVENT_START,
                StateEventKind::End => EVENT_END,
                StateEventKind::Complete => EVENT_COMPLETE,
                StateEventKind::Event(_) => continue,
            };
            if self.events.len() == MAX_EVENTS {
                self.events.remove(0);
            }
            self.events.push([kind, e.track as i32, e.animation as i32]);
        }
    }

    fn geometry_bounds(&self) -> [f32; 4] {
        let mut b = [f32::MAX, f32::MAX, f32::MIN, f32::MIN];
        for &[x, y] in self.positions.as_chunks::<2>().0 {
            b = [b[0].min(x), b[1].min(y), b[2].max(x), b[3].max(y)];
        }
        if b[0] > b[2] {
            [0.0; 4]
        } else {
            b
        }
    }
}

/// Worst-case vertices and indices: every slot showing its largest attachment.
fn capacity(data: &SkeletonData) -> Result<(usize, usize), String> {
    let mut vertices = 0;
    let mut indices = 0;
    for (i, slot) in data.slots.iter().enumerate() {
        if slot.blend != Blend::Normal {
            return Err(format!("skeleton: slot {} uses a blend mode other than normal", slot.name));
        }
        if slot.dark.is_some() {
            return Err(format!("skeleton: slot {} uses two-color tint", slot.name));
        }
        let skins = data.skins.iter().flat_map(|s| s.slots.iter().filter(|e| e.0 == i).flat_map(|e| e.1.iter()));
        let (mut v, mut n) = (0, 0);
        for a in slot.attachments.iter().chain(skins) {
            let (av, an) = match &a.kind {
                AttachmentKind::Region { .. } => (4, 6),
                AttachmentKind::Mesh { uvs, triangles, .. } => (uvs.len() / 2, triangles.len()),
                AttachmentKind::LinkedMesh { .. } => return Err("skeleton: linked meshes are not supported".into()),
                AttachmentKind::Clipping { .. } => return Err("skeleton: clipping is not supported".into()),
                _ => (0, 0),
            };
            v = v.max(av);
            n = n.max(an);
        }
        vertices += v;
        indices += n;
    }
    Ok((vertices, indices))
}
