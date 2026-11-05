package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.BeforeClass
import org.junit.Test

/**
 * The real JNI library (libjarvis_rig, built for the host by :app's buildRigNativeHost) with the
 * bundled character: loading, the contract's animations, playback through the player, geometry,
 * malformed input and release. Plain JUnit, not Robolectric: a native library binds to one class
 * loader, and Robolectric's sandboxes are separate ones.
 */
class NativeRigRuntimeTest {
    private val skeleton = RigAssetsFixture.bytes(RigAssets.SKELETON)
    private val atlas = RigAssetsFixture.bytes(RigAssets.ATLAS)
    private val contract = RigAssetsFixture.contract()

    private fun open(): NativeRigRuntime = NativeRigRuntime.open(skeleton, atlas).getOrThrow()

    @Test
    fun `the bundled character loads with a sane frame and capacity`() {
        open().use { rig ->
            val info = rig.info
            assertTrue(info.vertexCapacity in 100..1_000)
            assertTrue(info.indexCapacity >= info.vertexCapacity)
            assertEquals(28, info.animationCount)
            assertTrue(info.maxX - info.minX in 200f..240f)
            assertTrue(info.maxY - info.minY in 290f..320f)
            assertEquals(512f, info.pageWidth)
        }
    }

    @Test
    fun `every animation the contract names is in the rig`() {
        open().use { rig ->
            for (name in contract.animationNames) assertTrue(name, rig.animationIndex(name) >= 0)
            assertEquals(-1, rig.animationIndex("idle_bored"))
            assertEquals(-1, rig.animationIndex(""))
        }
    }

    @Test
    fun `the player drives the real runtime through a transition into its idle`() {
        val rig = open()
        val player = requireNotNull(RigCharacterPlayer.create(rig, contract))
        player.use {
            it.start(Expression.DEFAULT, entrance = true)
            it.show(Expression.HAPPY)
            assertEquals(rig.animationIndex("to_happy"), rig.current(0))
            repeat(90) { _ -> it.advance(1f / 60f) }
            assertEquals(rig.animationIndex("idle_happy"), rig.current(0))
            assertEquals("the entrance cleared its track", -1, rig.current(1))
            assertTrue(it.triggerLongIdle())
            assertEquals(rig.animationIndex("long_idle_happy"), rig.current(0))
            it.show(Expression.SAD)
            assertEquals(rig.animationIndex("to_sad"), rig.current(0))
        }
    }

    @Test
    fun `geometry fits the preallocated buffers in every animation`() {
        open().use { rig ->
            val geometry = RigGeometry(rig.info)
            val events = IntArray(RigEvents.BUFFER)
            for (name in contract.animationNames) {
                assertTrue(rig.setAnimation(0, rig.animationIndex(name), true, 0f))
                var t = 0f
                while (t < contract.durations.getValue(name)) {
                    rig.update(0.1f, events)
                    assertFrameFits(rig, name, geometry)
                    t += 0.1f
                }
            }
        }
    }

    private fun assertFrameFits(
        rig: NativeRigRuntime,
        name: String,
        geometry: RigGeometry,
    ) {
        assertTrue(name, rig.build(geometry))
        assertTrue(geometry.vertexCount in 1..rig.info.vertexCapacity)
        assertTrue(geometry.indexCount % 3 == 0 && geometry.indexCount <= rig.info.indexCapacity)
        val vertices = geometry.vertexCount
        assertTrue((0 until geometry.indexCount).all { geometry.indices[it].toInt() and 0xFFFF < vertices })
    }

    @Test
    fun `undersized buffers are refused, not overrun`() {
        open().use { rig ->
            val tiny =
                RigGeometry(RigInfo(vertexCapacity = 3, indexCapacity = 3, minX = 0f, minY = 0f, maxX = 1f, maxY = 1f))
            assertFalse(rig.build(tiny))
        }
    }

    @Test
    fun `malformed input fails to load with a reason`() {
        assertLoadFails(ByteArray(0), atlas)
        assertLoadFails("not a skeleton".toByteArray(), atlas)
        assertLoadFails(skeleton.copyOf(skeleton.size / 2), atlas)
        assertLoadFails(skeleton, ByteArray(0))
        assertLoadFails(skeleton, byteArrayOf(0xC3.toByte(), 0x28))
        val withoutRegion = atlas.toString(Charsets.UTF_8).replace("eyes_closed\n", "eyes_shut\n").toByteArray()
        val failure = NativeRigRuntime.open(skeleton, withoutRegion).exceptionOrNull()
        assertTrue(failure?.message.orEmpty(), failure?.message.orEmpty().contains("eyes_closed"))
    }

    @Test
    fun `release is idempotent and use after it is refused`() {
        val rig = open()
        rig.close()
        rig.close()
        assertThrows(IllegalStateException::class.java) { rig.animationIndex("idle_default") }
        assertThrows(IllegalStateException::class.java) { rig.update(0f, IntArray(RigEvents.BUFFER)) }
    }

    @Test
    fun `many rigs can be opened and released`() {
        repeat(50) { open().close() }
    }

    private fun assertLoadFails(
        skeleton: ByteArray,
        atlas: ByteArray,
    ) {
        val result = NativeRigRuntime.open(skeleton, atlas)
        assertTrue(result.isFailure)
        assertNotNull(result.exceptionOrNull()?.message)
    }

    companion object {
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
