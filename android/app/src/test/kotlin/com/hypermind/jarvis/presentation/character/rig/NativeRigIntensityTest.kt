package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.BeforeClass
import org.junit.Test
import kotlin.math.hypot

/**
 * Presentation intensity through the real JNI library (host build) with the bundled rig and the
 * bundled intensity profile: it applies, neutral is bit-identical, no expression at any intensity
 * needs the guard (it stays a boundary for the unexpected), changes never jump, and bad values are
 * refused. Plain JUnit (see NativeRigRuntimeTest).
 */
class NativeRigIntensityTest {
    private val skeleton = RigAssetsFixture.bytes(RigAssets.SKELETON)
    private val atlas = RigAssetsFixture.bytes(RigAssets.ATLAS)
    private val contract = RigAssetsFixture.contract()
    private val profile = requireNotNull(RigIntensityProfile.parse(RigAssetsFixture.text(RigAssets.INTENSITY)))

    private fun open(): NativeRigRuntime = NativeRigRuntime.open(skeleton, atlas).getOrThrow()

    /** A runtime with the profile applied, and its player. */
    private fun shaped(): Pair<NativeRigRuntime, RigCharacterPlayer> {
        val rig = open()
        assertNotNull(RigShaping.configure(rig, contract, profile))
        return rig to RigCharacterPlayer.create(rig, contract, profile = profile)!!
    }

    private fun frame(
        player: RigCharacterPlayer,
        geometry: RigGeometry,
    ): FloatArray {
        player.advance(FRAME)
        assertTrue(player.build(geometry))
        return geometry.positions.copyOf(geometry.vertexCount * 2)
    }

    @Test
    fun `the bundled profile applies to the real rig, and every bone it names exists`() {
        val (rig, player) = shaped()
        player.use {
            assertEquals(14, rig.info.boneCount)
            for (e in Expression.entries) {
                for (b in profile.postureHolds.getValue(e) + profile.motionHolds.getValue(e)) {
                    assertTrue(
                        b,
                        rig.boneIndex(b) >= 0,
                    )
                }
            }
            assertEquals(-1, rig.boneIndex("tail"))
            val info = requireNotNull(rig.shapeInfo())
            assertTrue(info.configured)
            assertFalse("nothing to do until an intensity other than neutral is shown", info.active)
        }
    }

    @Test
    fun `neutral intensities are bit-identical to an unshaped rig`() {
        val plainRig = open()
        val plain = RigCharacterPlayer.create(plainRig, contract)!!
        val (_, shapedPlayer) = shaped()
        val (g1, g2) = RigGeometry(plainRig.info) to RigGeometry(plainRig.info)
        listOf(plain, shapedPlayer).forEach { it.start(Expression.DEFAULT, 5, entrance = true) }
        val script: List<Pair<Int, (RigCharacterPlayer) -> Unit>> =
            listOf(
                30 to { p -> p.show(Expression.HAPPY, 5) },
                200 to { p -> p.triggerLongIdle() },
                600 to { p -> p.show(Expression.SAD, 6) },
                800 to { p -> p.show(Expression.SLEEPY, 5) },
                1000 to { p -> p.exit() },
            )
        for (f in 0 until 1100) {
            for ((at, action) in script) if (at == f) listOf(plain, shapedPlayer).forEach(action)
            assertArrayEquals("frame $f", frame(plain, g1), frame(shapedPlayer, g2), 0f)
        }
        plain.close()
        shapedPlayer.close()
    }

    /** What the guard did in one session, through its entry and regular idle, and in all. */
    private data class Session(
        val throughIdle: NativeRigRuntime.ShapeInfo,
        val total: NativeRigRuntime.ShapeInfo,
        val changedPose: Boolean,
    )

    /** One expression at one intensity: its entry from Default, its idle, its long idle, its idle again. */
    private fun session(
        state: Expression,
        intensity: Int,
    ): Session {
        val (rig, player) = shaped()
        val canonical = RigCharacterPlayer.create(open(), contract)!!
        try {
            return play(rig, player, canonical, state, intensity)
        } finally {
            player.close()
            canonical.close()
        }
    }

    private fun play(
        rig: NativeRigRuntime,
        player: RigCharacterPlayer,
        canonical: RigCharacterPlayer,
        state: Expression,
        intensity: Int,
    ): Session {
        val players = listOf(player, canonical)
        val (g, gc) = RigGeometry(rig.info) to RigGeometry(rig.info)
        players.forEach { it.start(Expression.DEFAULT, 5, entrance = false) }
        players.forEach { it.show(state, intensity) }
        val longIdle = contract.durations.getValue(contract.longIdle.getValue(state))
        val idleEnds = (IDLE_SECONDS / FRAME).toInt()
        var throughIdle: NativeRigRuntime.ShapeInfo? = null
        var differs = false
        for (f in 0 until ((IDLE_SECONDS + longIdle + 2f) / FRAME).toInt()) {
            if (f == idleEnds) {
                throughIdle = rig.shapeInfo()
                players.forEach { assertTrue(it.triggerLongIdle()) }
            }
            if (!frame(player, g).contentEquals(frame(canonical, gc))) differs = true
        }
        return Session(requireNotNull(throughIdle), requireNotNull(rig.shapeInfo()), differs)
    }

    private fun where(
        rig: NativeRigRuntime,
        info: NativeRigRuntime.ShapeInfo,
    ): String {
        if (info.clamps == 0L) return "none"
        val bone = FakeRigRuntime.BONES.firstOrNull { rig.boneIndex(it) == info.largestBone } ?: "${info.largestBone}"
        return "${info.clamps}x, largest ${"%.4f".format(info.largestClamp)} at $bone.${CHANNELS[info.largestChannel]}"
    }

    @Test
    fun `no expression at any intensity needs the guard`() {
        val rig = open()
        val results = Expression.entries.flatMap { e -> (1..10).map { i -> Triple(e, i, session(e, i)) } }
        // The intervention report (docs/CHARACTER_RIG.md §5.4): requested gains, what the guard
        // trimmed through the entry and regular idle, and in the whole session with a long idle.
        println(
            results.joinToString("\n") { (e, i, r) ->
                val g = profile.gains(e, i)
                "${e.wire} $i: posture ${g.posture} motion ${g.motion}; entry+idle ${where(rig, r.throughIdle)}" +
                    "; with long idle ${where(rig, r.total)}; applied/requested " +
                    "%.5f".format(if (r.total.requested > 0f) r.total.applied / r.total.requested else 1f)
            },
        )
        rig.close()
        for ((e, i, r) in results) {
            val neutral = profile.gains(e, i).neutral
            assertEquals("$e $i changes the pose exactly when a gain is not 1", !neutral, r.changedPose)
            if (neutral) assertEquals("$e $i: the canonical animation is never clamped", 0L, r.total.clamps)
            // The guard is a boundary for the unexpected, not part of the look: what it may trim is a
            // rounding-size remainder (sad's forearm sway while idle_sad crossfades, 0.005).
            assertTrue("$e $i entry+idle: ${r.throughIdle}", r.throughIdle.largestClamp <= GUARD_REMAINDER)
            assertTrue("$e $i with long idle: ${r.total}", r.total.largestClamp <= GUARD_REMAINDER)
        }
    }

    @Test
    fun `changing intensity in an idle, or expression, never jumps`() {
        fun largestStep(
            p: RigCharacterPlayer,
            g: RigGeometry,
            changes: Map<Int, Int>,
        ): Float {
            p.start(Expression.HAPPY, 5, entrance = false)
            var previous = frame(p, g)
            var largest = 0f
            for (f in 1 until 900) {
                changes[f]?.let { i -> p.show(if (f >= 600) Expression.SAD else Expression.HAPPY, i) }
                val now = frame(p, g)
                if (now.size == previous.size) {
                    for (k in now.indices step 2) {
                        largest =
                            maxOf(largest, hypot(now[k] - previous[k], now[k + 1] - previous[k + 1]))
                    }
                }
                previous = now
            }
            return largest
        }
        val changes = mapOf(60 to 10, 200 to 2, 260 to 10, 400 to 1, 600 to 9, 700 to 3)
        val canonicalRig = open()
        val canonical =
            largestStep(RigCharacterPlayer.create(canonicalRig, contract)!!, RigGeometry(canonicalRig.info), changes)
        val (rig, player) = shaped()
        val layered = largestStep(player, RigGeometry(rig.info), changes)
        assertTrue("with layers $layered vs canonical $canonical", layered <= canonical * 1.05f + 0.25f)
        canonicalRig.close()
        player.close()
    }

    @Test
    fun `the real library refuses bad values and calls after release`() {
        val rig = open()
        assertFalse("nothing to set before configuring", rig.setShape(rig.animationIndex("idle_happy"), 1.2f, 1.2f))
        val idles = Expression.entries.map { rig.animationIndex(contract.idle.getValue(it)) }.toIntArray()
        val range = (contract.idle.values + contract.longIdle.values).map { rig.animationIndex(it) }.toIntArray()
        val weights = FloatArray(idles.size * rig.info.boneCount) { 1f }
        val reference = rig.animationIndex("idle_default")
        assertFalse(rig.configureShape(reference, idles, range, weights.copyOf(3), weights, 0.8f, 0.5f))
        assertFalse(rig.configureShape(reference, intArrayOf(-1), range, weights, weights, 0.8f, 0.5f))
        assertFalse(rig.configureShape(rig.animationIndex("to_happy"), idles, range, weights, weights, 0.8f, 0.5f))
        assertFalse(rig.configureShape(reference, idles, range, weights, weights, Float.NaN, 0.5f))
        assertFalse(rig.configureShape(reference, idles, range, FloatArray(weights.size) { 2f }, weights, 0.8f, 0.5f))
        assertFalse(rig.shapeInfo()!!.configured)
        assertTrue(rig.configureShape(reference, idles, range, weights, weights, 0.8f, 0.5f))
        val happy = rig.animationIndex("idle_happy")
        assertFalse(rig.setShape(happy, Float.NaN, 1f))
        assertFalse(rig.setShape(happy, 1f, -0.5f))
        assertFalse(rig.setShape(happy, 3.5f, 1f))
        assertFalse("not an idle", rig.setShape(rig.animationIndex("to_happy"), 1.2f, 1.2f))
        assertFalse(rig.setShape(-2, 1f, 1f))
        assertFalse(rig.shapeInfo()!!.active)
        assertTrue(rig.setShape(happy, 1.2f, 1.2f))
        assertTrue(rig.shapeInfo()!!.active)
        assertTrue(rig.setShape(-1, 1f, 1f))
        rig.close()
        assertThrows(IllegalStateException::class.java) { rig.boneIndex("head") }
        assertThrows(IllegalStateException::class.java) { rig.setShape(happy, 1f, 1f) }
        assertThrows(IllegalStateException::class.java) {
            rig.configureShape(reference, idles, range, weights, weights, 0.8f, 0.5f)
        }
        assertThrows(IllegalStateException::class.java) { rig.shapeInfo() }
    }

    companion object {
        private const val FRAME = 1f / 60f

        /** The entry and regular idle: the session's first seconds. */
        private const val IDLE_SECONDS = 3f

        /** The most the guard may trim anywhere (degrees or units): rounding-size, invisible. */
        private const val GUARD_REMAINDER = 0.01f

        private val CHANNELS = listOf("x", "y", "rotation", "scaleX", "scaleY", "shearX", "shearY")

        @JvmStatic
        @BeforeClass
        fun loadHostLibrary() {
            val path = System.getProperty("jarvis.rig.native.lib")
            val required = System.getProperty("jarvis.rig.native.required") == "true"
            if (path.isNullOrEmpty() && required) error("the host native library is required but was not built")
            assumeTrue("no host native library (built with -Pjarvis.rig.native=true)", !path.isNullOrEmpty())
            assertTrue(RigNative.loadForTests { System.load(path!!) })
            assertNull(RigNative.loadError)
        }
    }
}
