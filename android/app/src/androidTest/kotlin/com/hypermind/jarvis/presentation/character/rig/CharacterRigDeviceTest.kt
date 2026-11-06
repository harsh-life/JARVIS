package com.hypermind.jarvis.presentation.character.rig

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
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
import com.hypermind.jarvis.presentation.ui.CHARACTER_TAG
import com.hypermind.jarvis.presentation.ui.Character
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
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

    private fun load(reducedMotion: Boolean = false): LoadedRig =
        requireNotNull(
            RigAssets.load({ context.assets.open(it) }, reducedMotion = reducedMotion),
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

    // --- presentation intensity (docs/CHARACTER_RIG.md §5) --------------------------------------

    private fun silhouetteIou(
        a: Bitmap,
        b: Bitmap,
    ): Float {
        var both = 0
        var either = 0
        for (y in 0 until a.height) {
            for (x in 0 until a.width) {
                val p = Color.alpha(a.getPixel(x, y)) > 128
                val q = Color.alpha(b.getPixel(x, y)) > 128
                if (p && q) both++
                if (p || q) either++
            }
        }
        return both.toFloat() / either
    }

    /** Frames of [state] at [intensity] once its entry and ramps are over, across one idle loop. */
    private fun idleFrames(
        state: Expression,
        intensity: Int,
    ): Pair<List<Bitmap>, List<FloatArray>> =
        load().use { rig ->
            val geometry = RigGeometry(rig.info)
            rig.player.start(Expression.DEFAULT, 5, entrance = false)
            rig.player.show(state, intensity)
            repeat(SETTLE_FRAMES) { rig.player.advance(1f / 60f) }
            val bitmaps = mutableListOf<Bitmap>()
            val poses = mutableListOf<FloatArray>()
            repeat(PHASES) {
                repeat(PHASE_FRAMES) { rig.player.advance(1f / 60f) }
                assertTrue(rig.player.build(geometry))
                bitmaps += draw(rig, geometry)
                poses += geometry.positions.copyOf(geometry.vertexCount * 2)
            }
            bitmaps to poses
        }

    /** Mean distance of every vertex from its own average over the frames: how much the idle moves. */
    private fun movement(poses: List<FloatArray>): Float {
        val n = poses.first().size
        val mean = FloatArray(n) { k -> poses.sumOf { it[k].toDouble() }.toFloat() / poses.size }
        var sum = 0.0
        for (p in poses) {
            for (k in 0 until n step 2) {
                sum +=
                    Math.hypot((p[k] - mean[k]).toDouble(), (p[k + 1] - mean[k + 1]).toDouble())
            }
        }
        return (sum / (poses.size * n / 2)).toFloat()
    }

    @Test
    fun everyExpressionAtLowNeutralAndHighIntensityStaysRecognisable() {
        val dir = File(out, "intensity").apply { mkdirs() }
        val levels = listOf(1, 5, 10)
        val overview = Bitmap.createBitmap(SIZE * levels.size, SIZE * Expression.entries.size, Bitmap.Config.ARGB_8888)
        val sheetCanvas = Canvas(overview).apply { drawColor(Color.WHITE) }
        val label =
            Paint().apply {
                textSize = 22f
                color = Color.BLACK
                isAntiAlias = true
            }
        val report = StringBuilder()
        for ((row, e) in Expression.entries.withIndex()) {
            val frames = levels.associateWith { idleFrames(e, it) }
            val neutral = frames.getValue(5)
            val sheet = Bitmap.createBitmap(SIZE * levels.size, SIZE * PHASES, Bitmap.Config.ARGB_8888)
            val c = Canvas(sheet).apply { drawColor(Color.WHITE) }
            for ((col, level) in levels.withIndex()) {
                val (bitmaps, poses) = frames.getValue(level)
                for ((k, f) in bitmaps.withIndex()) {
                    assertTrue("${e.wire} $level is not blank", f.opaque() > SIZE * SIZE / 8)
                    // The same moment at neutral: the expression must stay recognisable.
                    val iou = silhouetteIou(f, neutral.first[k])
                    assertTrue("${e.wire} $level phase $k IoU vs neutral $iou", iou > MIN_IOU_VS_NEUTRAL)
                    save(f, "intensity/${e.wire}-$level-$k")
                    c.drawBitmap(f, (col * SIZE).toFloat(), (k * SIZE).toFloat(), null)
                }
                c.drawText("${e.wire} $level", col * SIZE + 8f, 26f, label)
                sheetCanvas.drawBitmap(bitmaps.first(), (col * SIZE).toFloat(), (row * SIZE).toFloat(), null)
                sheetCanvas.drawText("${e.wire} $level", col * SIZE + 8f, row * SIZE + 26f, label)
                report.append("${e.wire} $level: movement ${"%.3f".format(movement(poses))}\n")
            }
            save(sheet, "intensity/sheet-${e.wire}")
            // More intensity, more idle motion; less, less.
            val (low, mid, high) = levels.map { movement(frames.getValue(it).second) }
            assertTrue("${e.wire} motion $low < $mid < $high", low < mid && mid < high)
        }
        save(overview, "intensity/overview")
        android.util.Log.i(TAG, "idle movement by intensity:\n$report")
        assertTrue(File(dir, "overview.png").length() > 0)
    }

    private val contract: RigContract by lazy {
        val text =
            context.assets.open("${RigAssets.ROOT}/${RigAssets.CONTRACT}").use {
                it.readBytes().toString(Charsets.UTF_8)
            }
        requireNotNull(RigContract.parse(text))
    }

    /** Rows of labelled frames as one contact sheet under `intensity/`, each frame [cell] px. */
    private fun sheet(
        name: String,
        rows: List<Pair<String, List<Bitmap>>>,
        cell: Int = SIZE,
    ) {
        val cols = rows.maxOf { it.second.size }
        val sheet = Bitmap.createBitmap(cell * cols, cell * rows.size, Bitmap.Config.ARGB_8888)
        val c = Canvas(sheet).apply { drawColor(Color.WHITE) }
        val label =
            Paint().apply {
                textSize = 16f
                color = Color.BLACK
                isAntiAlias = true
            }
        for ((r, row) in rows.withIndex()) {
            for ((k, f) in row.second.withIndex()) {
                val x = k * cell
                val y = r * cell
                c.drawBitmap(f, null, android.graphics.Rect(x, y, x + cell, y + cell), null)
            }
            c.drawText(row.first, 6f, r * cell + 18f, label)
        }
        save(sheet, "intensity/$name")
    }

    /**
     * Frames every [stepFrames] of a session: [setup] runs, then [frames] frames advance, with
     * [at] actions keyed by frame. Every frame drawn is checked to be a whole character.
     */
    private fun timeline(
        setup: (CharacterAnimationPlayer) -> Unit,
        frames: Int,
        stepFrames: Int,
        at: Map<Int, (CharacterAnimationPlayer) -> Unit> = emptyMap(),
    ): List<Bitmap> =
        load().use { rig ->
            val geometry = RigGeometry(rig.info)
            setup(rig.player)
            val out = mutableListOf<Bitmap>()
            for (f in 0 until frames) {
                at[f]?.invoke(rig.player)
                rig.player.advance(1f / 60f)
                if (f % stepFrames == 0) {
                    assertTrue(rig.player.build(geometry))
                    out += draw(rig, geometry).also { assertTrue("frame $f is whole", it.opaque() > SIZE * SIZE / 8) }
                }
            }
            out
        }

    @Test
    fun longIdlesAndIntensityChangesAreCapturedWholeAndRecognisable() {
        File(out, "intensity").mkdirs()
        // Further intensities: settled idle frames, recognisable against the same moment at 5.
        val extra = listOf(Expression.SLEEPY to 2, Expression.SLEEPY to 8, Expression.ANGRY to 1, Expression.ANGRY to 4)
        val rows =
            (extra + (Expression.ANGRY to 10) + (Expression.SAD to 10)).map { (e, i) ->
                val (bitmaps, _) = idleFrames(e, i)
                val neutral = idleFrames(e, 5).first
                for ((k, f) in bitmaps.withIndex()) {
                    val iou = silhouetteIou(f, neutral[k])
                    assertTrue("${e.wire} $i phase $k IoU vs neutral $iou", iou > MIN_IOU_VS_NEUTRAL)
                    save(f, "intensity/${e.wire}-$i-$k")
                }
                "${e.wire} $i" to bitmaps
            }
        sheet("extra-levels", rows)
        // The regular idle into its long idle and back, at a strong and a weak intensity: frames
        // every 0.1 s around each crossfade (into: 0.3 s; back: 0.4 s, the posture easing in after).
        val longIdle = mutableListOf<Pair<String, List<Bitmap>>>()
        for ((e, i) in listOf(Expression.SLEEPY to 10, Expression.ANGRY to 1)) {
            val trigger =
                mapOf(
                    TRIGGER_FRAME to
                        { p: CharacterAnimationPlayer -> assertTrue("the long idle starts", p.triggerLongIdle()) },
                )
            val into = timeline({ it.start(e, i, entrance = false) }, TRIGGER_FRAME + INTO_FRAMES, SAMPLE, trigger)
            longIdle += "${e.wire} $i: 0.5 s before the long idle, then every 0.1 s" to into.drop(BEFORE_TRIGGER)
            val long = (contract.durations.getValue(contract.longIdle.getValue(e)) * 60).toInt()
            val back =
                timeline({ it.start(e, i, entrance = false) }, TRIGGER_FRAME + long + BACK_FRAMES, SAMPLE, trigger)
            longIdle += "${e.wire} $i: from 1 s before the idle returns, every 0.1 s" to back.takeLast(RETURN_SAMPLES)
        }
        sheet("long-idle", longIdle, cell = SIZE / 2)

        // Intensity changing while an expression shows: happy 2 -> 10 -> 2 and sad 1 -> 10, every
        // 0.1 s through the posture ramp; nothing restarts, the pose eases.
        fun change(
            e: Expression,
            from: Int,
            to: Int,
        ) = "${e.wire} $from -> $to: the change, then every 0.1 s" to
            timeline(
                { it.start(e, from, entrance = false) },
                SETTLE_FRAMES + CHANGE_FRAMES,
                SAMPLE,
                mapOf(SETTLE_FRAMES to { p: CharacterAnimationPlayer -> p.show(e, to) }),
            ).drop(SETTLE_FRAMES / SAMPLE)
        sheet(
            "intensity-changes",
            listOf(change(Expression.HAPPY, 2, 10), change(Expression.HAPPY, 10, 2), change(Expression.SAD, 1, 10)),
            cell = SIZE / 2,
        )
    }

    @Test
    fun reducedMotionIgnoresIntensity() {
        val frames =
            listOf(1, 5, 10).map { i ->
                load(reducedMotion = true).use { rig ->
                    val geometry = RigGeometry(rig.info)
                    rig.player.start(Expression.SAD, i, entrance = true)
                    repeat(120) { rig.player.advance(1f / 60f) }
                    assertTrue(rig.player.build(geometry))
                    geometry.positions.copyOf(geometry.vertexCount * 2)
                }
            }
        assertArrayEquals(frames[1], frames[0], 0f)
        assertArrayEquals(frames[1], frames[2], 0f)
    }

    @Test
    fun pauseHoldsTheIntensityLayersStillAndResumeContinues() {
        load().use { rig ->
            val geometry = RigGeometry(rig.info)
            rig.player.start(Expression.SLEEPY, 2, entrance = false)
            repeat(30) { rig.player.advance(1f / 60f) }
            rig.player.show(Expression.SLEEPY, 10) // ramping when the app goes to the background
            rig.player.advance(1f / 60f)
            rig.player.pause()
            rig.player.build(geometry)
            val paused = geometry.positions.copyOf(geometry.vertexCount * 2)
            repeat(120) { rig.player.advance(1f / 60f) }
            rig.player.build(geometry)
            assertArrayEquals(
                "time stands still while paused",
                paused,
                geometry.positions.copyOf(
                    geometry.vertexCount * 2,
                ),
                0f,
            )
            rig.player.resume()
            rig.player.advance(1f / 60f)
            rig.player.build(geometry)
            assertArrayEquals(
                "the first frame after resume does not jump",
                paused,
                geometry.positions.copyOf(
                    geometry.vertexCount * 2,
                ),
                0f,
            )
            repeat(60) { rig.player.advance(1f / 60f) }
            rig.player.build(geometry)
            assertFalse(paused.contentEquals(geometry.positions.copyOf(geometry.vertexCount * 2)))
        }
    }

    @Test
    fun withoutTheRigTheStaticImagesShowAtAnyIntensity() {
        val intensity = MutableStateFlow(1)
        compose.setContent {
            val i by intensity.collectAsState()
            Character(
                expression = Expression.SAD,
                profile = CharacterProfile.BUILT_IN,
                settings = CharacterSettings(),
                rig = RigLoader { _, _ -> null },
                intensity = i,
            )
        }
        for (i in listOf(1, 10)) {
            intensity.value = i
            compose.waitUntil(TIMEOUT) {
                compose
                    .onAllNodesWithTag(
                        "$CHARACTER_TAG/sad.webp",
                        useUnmergedTree = true,
                    ).fetchSemanticsNodes()
                    .isNotEmpty()
            }
        }
    }

    private companion object {
        const val TAG = "JarvisRigTest"
        const val SIZE = 360
        const val TIMEOUT = 20_000L
        const val PADDED = 1f + 2f * RigPainter.PADDING

        /** Entry, idle hand-over and both ramps are over by then (2.5 s). */
        const val SETTLE_FRAMES = 150

        /** Frames captured across the idle, this many frames apart (0.4 s). */
        const val PHASES = 4
        const val PHASE_FRAMES = 24

        /** Silhouette overlap with the same moment at intensity 5. */
        const val MIN_IOU_VS_NEUTRAL = 0.85f

        /** Timelines: a frame every 0.1 s; the long idle triggered at 1.5 s. */
        const val SAMPLE = 6
        const val TRIGGER_FRAME = 90
        const val BEFORE_TRIGGER = (TRIGGER_FRAME - 30) / SAMPLE
        const val INTO_FRAMES = 60
        const val BACK_FRAMES = 84
        const val RETURN_SAMPLES = 24
        const val CHANGE_FRAMES = 90
    }
}
