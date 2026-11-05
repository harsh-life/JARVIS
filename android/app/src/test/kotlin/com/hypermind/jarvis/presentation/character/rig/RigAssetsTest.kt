package com.hypermind.jarvis.presentation.character.rig

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.GraphicsMode
import java.io.ByteArrayInputStream
import java.io.FileNotFoundException
import java.io.InputStream

/**
 * Loading the bundled rig, and every way it can fail: each failure is "no rig", so the screen
 * keeps the static images (CharacterViewTest), never a crash or a half-loaded character.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class RigAssetsTest {
    private val contract = RigAssetsFixture.contract()
    private var runtimeOpened = 0
    private val fakeRuntime: (ByteArray, ByteArray) -> Result<RigRuntime> = { _, _ ->
        runtimeOpened++
        Result.success(FakeRigRuntime.forContract(contract))
    }

    private fun load(
        open: (String) -> InputStream = RigAssetsFixture::open,
        runtime: (ByteArray, ByteArray) -> Result<RigRuntime> = fakeRuntime,
    ): LoadedRig? = RigAssets.load(open, reducedMotion = false, runtime = runtime)

    private fun without(name: String): (String) -> InputStream = { path ->
        if (path.endsWith("/$name")) throw FileNotFoundException(path) else RigAssetsFixture.open(path)
    }

    private fun replacing(
        name: String,
        bytes: ByteArray,
    ): (
        String,
    ) -> InputStream = { path ->
        if (path.endsWith("/$name")) ByteArrayInputStream(bytes) else RigAssetsFixture.open(path)
    }

    @Test
    fun `the bundled rig loads with its page and a ready player`() {
        val rig = load()
        assertNotNull(rig)
        rig!!.use {
            assertEquals(512, it.page.width)
            assertEquals(512, it.page.height)
            assertEquals(com.hypermind.jarvis.presentation.character.Expression.DEFAULT, it.player.expression)
            assertTrue(!it.player.released)
        }
        assertTrue(rig.player.released)
        assertTrue(rig.page.isRecycled)
    }

    @Test
    fun `any missing file means no rig`() {
        for (name in listOf(RigAssets.SKELETON, RigAssets.ATLAS, RigAssets.PAGE, RigAssets.CONTRACT)) {
            assertNull(name, load(open = without(name)))
        }
    }

    @Test
    fun `a malformed contract means no rig, and the runtime is never opened`() {
        assertNull(load(open = replacing(RigAssets.CONTRACT, "{ not json".toByteArray())))
        assertNull(load(open = replacing(RigAssets.CONTRACT, "{}".toByteArray())))
        assertEquals(0, runtimeOpened)
    }

    @Test
    fun `an undecodable page means no rig, and the runtime it opened is released`() {
        val opened = mutableListOf<FakeRigRuntime>()
        val rig =
            load(
                open = replacing(RigAssets.PAGE, "not a png".toByteArray()),
                runtime = { _, _ -> Result.success(FakeRigRuntime.forContract(contract).also { opened += it }) },
            )
        assertNull(rig)
        assertTrue(opened.all { it.closed == 1 })
    }

    @Test
    fun `a page that is not the atlas's size means no rig`() {
        val small = android.graphics.Bitmap.createBitmap(64, 64, android.graphics.Bitmap.Config.ARGB_8888)
        val png =
            java.io.ByteArrayOutputStream().use {
                small.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)
                it.toByteArray()
            }
        assertNull(load(open = replacing(RigAssets.PAGE, png)))
    }

    @Test
    fun `a runtime that cannot load means no rig`() {
        assertNull(load(runtime = { _, _ -> Result.failure(IllegalStateException("no native library")) }))
    }

    @Test
    fun `a rig missing an animation the contract names means no rig, released`() {
        val partial = FakeRigRuntime(contract.durations - "exit")
        assertNull(load(runtime = { _, _ -> Result.success(partial) }))
        assertEquals(1, partial.closed)
    }

    @Test
    fun `without the native library the bundled loader reports no rig`() {
        // Robolectric has no Android .so; the production loader must notice, not throw.
        assertTrue(!RigNative.available)
        assertNull(RigAssets.load(RigAssetsFixture::open, reducedMotion = false))
    }
}
