package com.hypermind.jarvis.presentation.character.rig

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.GraphicsMode

/**
 * The Android drawing of runtime geometry: world units (Y up) fitted into the view, texture
 * coordinates in page pixels. Drawn into a real bitmap with known geometry.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class RigPainterTest {
    /** A page whose top half is red and bottom half blue. */
    private val page =
        Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888).apply {
            for (y in 0 until 64) for (x in 0 until 64) setPixel(x, y, if (y < 32) Color.RED else Color.BLUE)
        }

    /** One quad over the whole frame, the page's top (v = 0) at the frame's top (max y). */
    private val frame =
        RigInfo(
            vertexCapacity = 4,
            indexCapacity = 6,
            minX = -50f,
            minY = 0f,
            maxX = 50f,
            maxY = 100f,
            pageWidth = 64f,
            pageHeight = 64f,
        )

    private fun quad(): RigGeometry =
        RigGeometry(frame).apply {
            floatArrayOf(-50f, 100f, 50f, 100f, 50f, 0f, -50f, 0f).copyInto(positions)
            floatArrayOf(0f, 0f, 64f, 0f, 64f, 64f, 0f, 64f).copyInto(texs)
            shortArrayOf(0, 1, 2, 0, 2, 3).copyInto(indices)
            vertexCount = 4
            indexCount = 6
        }

    private fun render(
        width: Int,
        height: Int,
    ): Bitmap {
        val out = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        RigPainter(page, frame).draw(Canvas(out), width.toFloat(), height.toFloat(), quad())
        return out
    }

    @Test
    fun `world Y up is drawn the right way up`() {
        val out = render(120, 120)
        assertEquals("top of the character", Color.RED, out.getPixel(60, 40))
        assertEquals("bottom of the character", Color.BLUE, out.getPixel(60, 80))
    }

    @Test
    fun `the frame is fitted, centred and keeps its aspect`() {
        val out = render(300, 120)
        // 100 x 100 units plus padding fit the height; the sides stay empty.
        assertEquals(Color.TRANSPARENT, out.getPixel(10, 60))
        assertEquals(Color.TRANSPARENT, out.getPixel(290, 60))
        assertTrue(Color.alpha(out.getPixel(150, 60)) == 255)
        val padded = 1f + 2f * RigPainter.PADDING
        val half = (120f / padded) / 2f
        assertEquals(Color.TRANSPARENT, out.getPixel((150 + half + 3).toInt(), 60))
        assertEquals(255, Color.alpha(out.getPixel((150 + half - 3).toInt(), 60)))
    }

    @Test
    fun `nothing is drawn for an empty frame or a zero-sized view`() {
        val empty = RigGeometry(frame)
        val out = Bitmap.createBitmap(50, 50, Bitmap.Config.ARGB_8888)
        RigPainter(page, frame).draw(Canvas(out), 50f, 50f, empty)
        RigPainter(page, frame).draw(Canvas(out), 0f, 0f, quad())
        for (y in 0 until 50) for (x in 0 until 50) assertEquals(Color.TRANSPARENT, out.getPixel(x, y))
    }
}
