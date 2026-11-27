package com.hypermind.jarvis.ui

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.BuildConfig
import com.hypermind.jarvis.R
import com.hypermind.jarvis.presentation.character.CharacterProfile
import com.hypermind.jarvis.presentation.character.Expression
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

/** The brand mark is the owner's satisfied-face original, framed the same everywhere. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class BrandMarkTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val characterDir = File(System.getProperty("jarvis.character.dir") ?: "../../assets/character")

    @Test
    fun `the brand mark is the built-in profile's satisfied image`() {
        val satisfied = CharacterProfile.BUILT_IN.assetFor(Expression.SATISFIED)
        assertEquals("${CharacterProfile.ASSET_ROOT}/default/$satisfied", BuildConfig.BRAND_ASSET)
    }

    @Test
    fun `the header draws the original byte for byte - not a copy or a redrawing`() {
        val packaged = context.assets.open(BuildConfig.BRAND_ASSET).use { it.readBytes() }
        assertArrayEquals(characterDir.resolve("default/satisfied.png").readBytes(), packaged)
    }

    @Test
    fun `the frame holds the whole hood and face`() {
        // The sticker's opaque extent around the hood in satisfied.png (843 x 1264): x 161..680,
        // from the top of the hood (y 241) down past the raised fists (y 660).
        val frame = BrandFrame.SOURCE
        assertTrue(frame.left <= 161 && frame.left + frame.side >= 681)
        assertTrue(frame.top <= 241 && frame.top + frame.side >= 660)
        assertNotNull(frame.visiblePart(843, 1264, 120f))
    }

    @Test
    fun `a frame running past the image places the overlap where it falls`() {
        val part = BrandFrame(left = -10, top = 20, side = 100).visiblePart(width = 80, height = 200, dstSide = 50f)!!
        assertEquals(BrandFrame.Part(0, 20, 80, 100, 5, 0, 40, 50), part)
        assertNull(BrandFrame(left = 90, top = 0, side = 10).visiblePart(80, 80, 10f))
    }

    @Test
    fun `decoding is reduced only while the frame stays at least the badge's size`() {
        val frame = BrandFrame(-19, 56, 880)
        assertEquals(1, frame.sampleSize(600))
        assertEquals(4, frame.sampleSize(120))
        assertEquals(1, frame.sampleSize(0))
        assertEquals(BrandFrame(-5, 14, 220), frame.scaledBy(4))
    }

    @Test
    fun `the launcher and notification icons are the rendered brand mark`() {
        assertEquals(R.mipmap.ic_launcher, context.applicationInfo.icon)
        val res = context.resources
        val foreground = BitmapFactory.decodeResource(res, R.mipmap.ic_launcher_foreground)
        assertNotNull(foreground)
        assertEquals(foreground.width, foreground.height)
        // The status-bar icon and the themed-icon layer are white on transparent: the platform tints them.
        assertWhiteOnTransparent(BitmapFactory.decodeResource(res, R.drawable.ic_stat_jarvis))
        val monochrome = BitmapFactory.decodeResource(res, R.mipmap.ic_launcher_monochrome)
        assertEquals(foreground.width, monochrome.width)
        assertEquals(foreground.height, monochrome.height)
        assertWhiteOnTransparent(monochrome)
    }

    private fun assertWhiteOnTransparent(icon: Bitmap) {
        var opaque = 0
        for (y in 0 until icon.height) {
            for (x in 0 until icon.width) {
                val p = icon.getPixel(x, y)
                // (premultiplied storage rounds faint pixels' colour, so judge the solid ones)
                if (p ushr 24 >= 0x40) {
                    opaque++
                    assertTrue("pixel $x,$y", listOf(16, 8, 0).all { (p shr it and 255) >= 0xF0 })
                }
            }
        }
        assertTrue(opaque > 0)
    }
}
