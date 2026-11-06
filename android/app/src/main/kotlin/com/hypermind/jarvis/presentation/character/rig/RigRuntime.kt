package com.hypermind.jarvis.presentation.character.rig

/**
 * The animation runtime as the player sees it: tracks, crossfades, a queue, time and triangle
 * geometry. [NativeRigRuntime] is the Rigby2D runtime over JNI; tests use a fake. Nothing outside
 * this package sees it.
 *
 * Not thread-safe: one owner calls it from one thread (the main thread in the app).
 */
interface RigRuntime : AutoCloseable {
    val info: RigInfo

    /** The animation's index, or -1. */
    fun animationIndex(name: String): Int

    /** Plays animation [index] on [track] now, crossfading over [mix] seconds and dropping its queue. */
    fun setAnimation(
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean

    /** Queues animation [index] after what [track] plays. */
    fun addAnimation(
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean

    fun clearTrack(track: Int)

    /** The animation [track] plays (not a queued one), or -1 when it is empty. */
    fun current(track: Int): Int

    /**
     * Advances [seconds] and poses the skeleton; writes the track callbacks since the last update
     * into [events] as ([RigEvents] kind, track, animation) triples. Returns how many, or -1.
     */
    fun update(
        seconds: Float,
        events: IntArray,
    ): Int

    /** Writes the current pose's triangles into [geometry]; false if it cannot (nothing to draw). */
    fun build(geometry: RigGeometry): Boolean

    /** The bone's index, or -1. */
    fun boneIndex(name: String): Int

    /**
     * Prepares the presentation-intensity layers once, while loading (docs/CHARACTER_RIG.md §5):
     * [idles] are the regular idles' animation indices (one per expression), [reference] one of
     * them, [range] the animations whose reach bounds the guard, and the weights one value in 0..1
     * per bone for each idle (0 keeps that bone canonical in that layer). False if refused; the
     * rig then plays canonically.
     */
    fun configureShape(
        reference: Int,
        idles: IntArray,
        range: IntArray,
        postureWeights: FloatArray,
        motionWeights: FloatArray,
        postureRampSeconds: Float,
        motionRampSeconds: Float,
    ): Boolean

    /**
     * Ramps the layers to these gains for idle animation [idle] (its expression); -1 ramps every
     * gain back to 1. Called only when the shown expression or intensity changes, never per frame.
     */
    fun setShape(
        idle: Int,
        postureGain: Float,
        motionGain: Float,
    ): Boolean
}

/** What a loaded rig needs to be drawn: worst-case buffer sizes and its setup-pose frame (world units, Y up). */
data class RigInfo(
    val vertexCapacity: Int,
    val indexCapacity: Int,
    val minX: Float,
    val minY: Float,
    val maxX: Float,
    val maxY: Float,
    val animationCount: Int = 0,
    val pageWidth: Float = 512f,
    val pageHeight: Float = 512f,
    val boneCount: Int = 0,
)

/**
 * One frame's triangles, in buffers allocated once for the rig's worst case: x/y pairs in world
 * units, texture coordinates in atlas-page pixels, 16-bit indices. Reused every frame.
 */
class RigGeometry(
    info: RigInfo,
) {
    val positions = FloatArray(info.vertexCapacity * 2)
    val texs = FloatArray(info.vertexCapacity * 2)
    val indices = ShortArray(info.indexCapacity)
    var vertexCount = 0
    var indexCount = 0

    /** Two ints filled by the native build: vertex and index counts. */
    internal val counts = IntArray(2)
}

/** Track callback kinds (rig.rs EVENT_*). */
object RigEvents {
    const val START = 0
    const val END = 1
    const val COMPLETE = 2

    /** Room for this many callbacks a frame (as triples). */
    const val BUFFER = 32 * 3
}
