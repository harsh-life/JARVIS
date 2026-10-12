package com.hypermind.jarvis.presentation.character

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.io.FileNotFoundException
import java.io.InputStream

/** OD-CHAR-1: the packaged artwork, decoded for real, and every loading fallback. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class CharacterAssetsTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val characterDir = File(System.getProperty("jarvis.character.dir") ?: "../../assets/character")
    private val open: (String) -> InputStream = { context.assets.open(it) }

    /** The approved originals' pixel sizes (assets/character/README.md). */
    private val sizes =
        mapOf(
            "default.png" to (427 to 585),
            "satisfied.webp" to (1024 to 1536),
            "happy.webp" to (1071 to 1469),
            "sleepy.webp" to (1361 to 1156),
            "surprised.webp" to (1024 to 1536),
            "angry.webp" to (1145 to 1374),
            "sad.webp" to (1145 to 1374),
        )

    @Test
    fun `the app carries the built-in profile`() {
        assertEquals(CharacterProfile.BUILT_IN, CharacterAssets.profile(context.assets))
    }

    @Test
    fun `the packaged images are the approved originals, byte for byte`() {
        for (asset in CharacterProfile.BUILT_IN.states.values) {
            val packaged = open("character/default/$asset").use { it.readBytes() }
            assertArrayEquals(asset, characterDir.resolve("default/$asset").readBytes(), packaged)
        }
    }

    @Test
    fun `every state decodes to its own image, with its size and transparency`() {
        val profile = CharacterProfile.BUILT_IN
        for (state in Expression.entries) {
            // A slot taller than every image: decoded at full size.
            val image = CharacterAssets.load(profile, state, targetHeightPx = 2000, open = open)
            assertNotNull("$state", image)
            assertEquals(profile.assetFor(state), image!!.asset)
            assertEquals("$state", sizes.getValue(image.asset), image.bitmap.width to image.bitmap.height)
            assertTrue("$state keeps its alpha", image.bitmap.hasAlpha())
            // The sticker's surroundings are transparent.
            assertEquals("$state corner", 0, image.bitmap.getPixel(0, 0) ushr 24)
        }
    }

    @Test
    fun `a small slot decodes a smaller bitmap`() {
        val image = CharacterAssets.load(CharacterProfile.BUILT_IN, Expression.SATISFIED, 300, open)!!
        assertEquals(1536 / 4, image.bitmap.height)
    }

    @Test
    fun `an image that cannot be read shows the default image`() {
        val missing: (String) -> InputStream = { path ->
            if (path.endsWith("sad.webp")) throw FileNotFoundException(path) else context.assets.open(path)
        }
        val image = CharacterAssets.load(CharacterProfile.BUILT_IN, Expression.SAD, 300, missing)
        assertEquals("default.png", image?.asset)

        val corrupt: (String) -> InputStream = { path ->
            if (path.endsWith("happy.webp")) "not an image".byteInputStream() else context.assets.open(path)
        }
        assertEquals("default.png", CharacterAssets.load(CharacterProfile.BUILT_IN, Expression.HAPPY, 300, corrupt)?.asset)
    }

    @Test
    fun `nothing loadable shows nothing, rather than failing`() {
        val none: (String) -> InputStream = { throw FileNotFoundException(it) }
        for (state in Expression.entries) assertNull(CharacterAssets.load(CharacterProfile.BUILT_IN, state, 300, none))
    }

    @Test
    fun `an unknown or unreadable profile is the built-in one`() {
        assertEquals(CharacterProfile.BUILT_IN, CharacterAssets.profile(context.assets, "no-such-profile"))
    }
}
