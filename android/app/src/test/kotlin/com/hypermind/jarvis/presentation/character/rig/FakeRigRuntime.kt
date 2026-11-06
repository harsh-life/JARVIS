package com.hypermind.jarvis.presentation.character.rig

/**
 * A [RigRuntime] that plays by the runtime's rules (set replaces and drops the queue, add queues,
 * a one-shot completes at its duration and hands over to the next queued entry, a loop never
 * ends) and records every call, so the player's choices are checked without native code.
 */
class FakeRigRuntime(
    private val durations: Map<String, Float>,
) : RigRuntime {
    private val names = durations.keys.toList()
    val calls = mutableListOf<String>()
    var updates = mutableListOf<Float>()
    var closed = 0
        private set

    private class Entry(
        val name: String,
        val loop: Boolean,
        var time: Float = 0f,
    )

    private val tracks = Array<Entry?>(2) { null }
    private val queues = Array(2) { ArrayDeque<Entry>() }
    private val pending = ArrayDeque<IntArray>()

    override val info =
        RigInfo(
            vertexCapacity = 8,
            indexCapacity = 12,
            minX = -100f,
            minY = 0f,
            maxX = 100f,
            maxY = 300f,
            boneCount = BONES.size,
        )

    override fun animationIndex(name: String): Int = names.indexOf(name)

    override fun setAnimation(
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean {
        val name = names.getOrNull(index) ?: return false
        calls += "set $track $name loop=$loop mix=$mix"
        queues[track].clear()
        tracks[track] = Entry(name, loop)
        return true
    }

    override fun addAnimation(
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean {
        val name = names.getOrNull(index) ?: return false
        calls += "add $track $name loop=$loop mix=$mix"
        if (tracks[track] == null) tracks[track] = Entry(name, loop) else queues[track].addLast(Entry(name, loop))
        return true
    }

    override fun clearTrack(track: Int) {
        calls += "clear $track"
        tracks[track] = null
        queues[track].clear()
    }

    override fun current(track: Int): Int = tracks[track]?.let { names.indexOf(it.name) } ?: -1

    override fun update(
        seconds: Float,
        events: IntArray,
    ): Int {
        updates += seconds
        for (track in tracks.indices) {
            val e = tracks[track] ?: continue
            e.time += seconds
            val d = durations.getValue(e.name)
            if (!e.loop && e.time >= d) {
                pending += intArrayOf(RigEvents.COMPLETE, track, names.indexOf(e.name))
                queues[track].removeFirstOrNull()?.let { tracks[track] = it }
            }
        }
        var n = 0
        while (pending.isNotEmpty() && (n + 1) * 3 <= events.size) {
            pending.removeFirst().copyInto(events, n * 3)
            n++
        }
        return n
    }

    /** Makes the runtime report [name] completing on [track] at the next update (a stray callback). */
    fun emitComplete(
        track: Int,
        name: String,
    ) {
        pending += intArrayOf(RigEvents.COMPLETE, track, names.indexOf(name))
    }

    /** Leaves [track] holding a finished one-shot with nothing queued (what a lost queue would do). */
    fun strand(
        track: Int,
        name: String,
    ) {
        tracks[track] = Entry(name, loop = false, time = Float.MAX_VALUE)
        queues[track].clear()
    }

    override fun build(geometry: RigGeometry): Boolean {
        geometry.vertexCount = 4
        geometry.indexCount = 6
        return true
    }

    /** The bones of the bundled rig, in its order (the fake knows names, not geometry). */
    var bones = BONES
    var acceptShape = true

    /** The last configuration, as the player's loader sent it. */
    var configured: ShapeConfiguration? = null
        private set

    data class ShapeConfiguration(
        val reference: Int,
        val idles: List<Int>,
        val range: List<Int>,
        val postureWeights: List<Float>,
        val motionWeights: List<Float>,
        val postureRamp: Float,
        val motionRamp: Float,
    )

    override fun boneIndex(name: String): Int = bones.indexOf(name)

    override fun configureShape(
        reference: Int,
        idles: IntArray,
        range: IntArray,
        postureWeights: FloatArray,
        motionWeights: FloatArray,
        postureRampSeconds: Float,
        motionRampSeconds: Float,
    ): Boolean {
        calls += "configure shape"
        if (!acceptShape) return false
        configured =
            ShapeConfiguration(
                reference,
                idles.toList(),
                range.toList(),
                postureWeights.toList(),
                motionWeights.toList(),
                postureRampSeconds,
                motionRampSeconds,
            )
        return true
    }

    override fun setShape(
        idle: Int,
        postureGain: Float,
        motionGain: Float,
    ): Boolean {
        val name = if (idle < 0) "none" else names.getOrNull(idle) ?: return false
        calls += "shape $name posture=$postureGain motion=$motionGain"
        return true
    }

    override fun close() {
        closed++
    }

    companion object {
        val BONES =
            listOf(
                "root",
                "body",
                "hip",
                "chest",
                "head",
                "face",
                "upperarm_r",
                "forearm_r",
                "hand_r",
                "upperarm_l",
                "forearm_l",
                "hand_l",
                "leg_r",
                "leg_l",
            )

        /** Every animation of the bundled contract, with its real duration. */
        fun forContract(contract: RigContract): FakeRigRuntime = FakeRigRuntime(contract.durations)
    }
}
