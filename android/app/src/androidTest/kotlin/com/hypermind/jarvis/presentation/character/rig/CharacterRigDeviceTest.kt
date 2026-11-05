package com.hypermind.jarvis.presentation.character.rig

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.os.SystemClock
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.hypermind.jarvis.presentation.character.CharacterProfile
import com.hypermind.jarvis.presentation.character.CharacterSettings
import com.hypermind.jarvis.presentation.character.Expression
import com.hypermind.jarvis.presentation.ui.Character
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * Instrumentation (needs a device or emulator): the APK's own native library and rig files, drawn
 * by the app's painter on a real Android canvas. Frames are saved under the app's files dir
 * (`rig-frames/`) for inspection; the frame cost is logged.
 */
@RunWith(AndroidJUnit4::class)
class CharacterRigDeviceTest {
    @get:Rule
    val compose = createComposeRule()

    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val out = File(context.filesDir, "rig-frames").apply { mkdirs() }

    private fun load(): LoadedRig =
        requireNotNull(
            RigAssets.load({ context.assets.open(it) }, reducedMotion = false),
        ) { "the bundled rig did not load" }

    private fun draw(
        rig: LoadedRig,
        geometry: RigGeometry,
        size: Int = SIZE,
    ): Bitmap {
        val bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        RigPainter(rig.page, rig.info).draw(Canvas(bitmap), size.toFloat(), size.toFloat(), geometry)
        return bitmap
    }

    private fun save(
        bitmap: Bitmap,
        name: String,
    ) = File(out, "$name.png").outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }

    private fun Bitmap.opaque(): Int =
        (0 until width * height).count {
            Color.alpha(getPixel(it % width, it / width)) >
                128
        }

    @Test
    fun theNativeLibraryLoadsOnThisAbi() {
        assertTrue(RigNative.loadError ?: "", RigNative.available)
    }

    @Test
    fun theSetupFrameHasTheArtworksSilhouette() {
        load().use { rig ->
            rig.player.start(Expression.DEFAULT, entrance = false)
            rig.player.advance(0f)
            val geometry = RigGeometry(rig.info)
            assertTrue(rig.player.build(geometry))
            val frame = draw(rig, geometry)
            save(frame, "setup")
            // default.png's figure, fitted the way the painter fits the rig's frame.
            val art = context.assets.open("character/default/default.png").use { BitmapFactory.decodeStream(it) }
            val expected = Bitmap.createBitmap(SIZE, SIZE, Bitmap.Config.ARGB_8888)
            val i = rig.info
            val scale = minOf(SIZE / ((i.maxX - i.minX) * PADDED), SIZE / ((i.maxY - i.minY) * PADDED))
            Canvas(expected).apply {
                // World origin is the figure's bottom centre: default.png pixel (213.5, 446).
                translate(SIZE / 2f, SIZE / 2f)
                scale(scale, scale)
                translate(-(i.minX + i.maxX) / 2f, (i.minY + i.maxY) / 2f)
                drawBitmap(art, -213.5f, -446f, null)
            }
            save(expected, "setup-expected")
            var both = 0
            var either = 0
            for (y in 0 until SIZE) {
                for (x in 0 until SIZE) {
                    val a = Color.alpha(frame.getPixel(x, y)) > 128
                    val b = Color.alpha(expected.getPixel(x, y)) > 128
                    if (a && b) both++
                    if (a || b) either++
                }
            }
            val iou = both.toFloat() / either
            android.util.Log.i(TAG, "setup silhouette IoU vs default.png: $iou")
            assertTrue("IoU $iou", iou > 0.95f)
        }
    }

    @Test
    fun everyExpressionRendersItsOwnFace() {
        load().use { rig ->
            val geometry = RigGeometry(rig.info)
            rig.player.start(Expression.DEFAULT, entrance = false)
            val frames = mutableMapOf<Expression, Bitmap>()
            for (e in Expression.entries) {
                rig.player.show(e)
                repeat(120) { rig.player.advance(1f / 60f) }
                assertTrue(rig.player.build(geometry))
                val f = draw(rig, geometry)
                assertTrue("${e.wire} is not blank", f.opaque() > SIZE * SIZE / 8)
                save(f, e.wire)
                frames[e] = f
            }
            val default = frames.getValue(Expression.DEFAULT)
            for ((e, f) in frames) if (e != Expression.DEFAULT) assertTrue("${e.wire} differs", !f.sameAs(default))
        }
    }

    @Test
    fun aFrameIsCheap() {
        load().use { rig ->
            val geometry = RigGeometry(rig.info)
            val bitmap = Bitmap.createBitmap(SIZE, SIZE, Bitmap.Config.ARGB_8888)
            val canvas = Canvas(bitmap)
            val painter = RigPainter(rig.page, rig.info)
            rig.player.start(Expression.HAPPY, entrance = false)
            repeat(60) { rig.player.advance(1f / 60f) }
            val frames = 600
            val start = SystemClock.elapsedRealtimeNanos()
            repeat(frames) {
                rig.player.advance(1f / 60f)
                rig.player.build(geometry)
                bitmap.eraseColor(0)
                painter.draw(canvas, SIZE.toFloat(), SIZE.toFloat(), geometry)
            }
            val perFrameMs = (SystemClock.elapsedRealtimeNanos() - start) / 1e6 / frames
            android.util.Log.i(TAG, "advance+build+software draw at ${SIZE}px: %.3f ms/frame".format(perFrameMs))
            assertTrue("$perFrameMs ms", perFrameMs < 8.0)
        }
    }

    @Test
    fun theScreenShowsTheRigAndFollowsTheExpression() {
        val expression = MutableStateFlow(Expression.DEFAULT)
        compose.mainClock.autoAdvance = false
        compose.setContent {
            val shown by expression.collectAsState()
            Character(expression = shown, profile = CharacterProfile.BUILT_IN, settings = CharacterSettings())
        }
        compose.waitUntil(TIMEOUT) {
            compose.mainClock.advanceTimeByFrame()
            compose.onAllNodesWithTag(RIG_TAG, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
        }
        for (e in listOf(Expression.HAPPY, Expression.SLEEPY, Expression.ANGRY, Expression.SAD, Expression.DEFAULT)) {
            expression.value = e
            repeat(90) { compose.mainClock.advanceTimeByFrame() }
            assertTrue(compose.onAllNodesWithTag(RIG_TAG, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty())
        }
    }

    private companion object {
        const val TAG = "JarvisRigTest"
        const val SIZE = 360
        const val TIMEOUT = 20_000L
        const val PADDED = 1f + 2f * RigPainter.PADDING
    }
}
