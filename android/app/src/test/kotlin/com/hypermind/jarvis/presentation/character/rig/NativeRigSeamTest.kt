package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.zip.Inflater

/**
 * The hood/sleeve seam through the real JNI library (docs/CHARACTER_RIG.md §5.5). The sleeves'
 * white sticker borders end in a straight cut at the shoulder; drawn over the hood's own white
 * border it merges, over the grey fur it is a hard white edge. Sleepy at intensity 10 once showed
 * it for as long as its idle played: the posture layer turned the head further while sleepy's
 * shoulders (held, at their authored extreme) could not follow. Plain JUnit (see NativeRigRuntimeTest).
 */
class NativeRigSeamTest {
    private val skeleton = RigAssetsFixture.bytes(RigAssets.SKELETON)
    private val atlasText = RigAssetsFixture.text(RigAssets.ATLAS)
    private val contract = RigAssetsFixture.contract()
    private val profileText = RigAssetsFixture.text(RigAssets.INTENSITY)
    private val probe = SeamProbe(RigAssetsFixture.bytes(RigAssets.PAGE), atlasText)

    /** Seam pixels over one settled loop of [state]'s regular idle: mean and largest frame. */
    private data class Seam(
        val mean: Double,
        val max: Int,
    )

    private fun seam(
        state: Expression,
        intensity: Int,
        profileText: String?,
    ): Seam {
        val rig = NativeRigRuntime.open(skeleton, atlasText.toByteArray()).getOrThrow()
        val profile = profileText?.let { requireNotNull(RigIntensityProfile.parse(it)) }
        if (profile != null) requireNotNull(RigShaping.configure(rig, contract, profile))
        val player = RigCharacterPlayer.create(rig, contract, profile = profile)!!
        player.use {
            val geometry = RigGeometry(rig.info)
            player.start(state, intensity, entrance = false)
            repeat(SETTLE_FRAMES) { player.advance(FRAME) } // every ramp has arrived
            val loop = contract.durations.getValue(contract.idle.getValue(state))
            var sum = 0L
            var max = 0
            var n = 0
            for (f in 0 until (loop / FRAME).toInt()) {
                player.advance(FRAME)
                if (f % SAMPLE_EVERY != 0) continue
                assertTrue(player.build(geometry))
                val count = probe.count(geometry, rig.info)
                sum += count
                max = maxOf(max, count)
                n++
            }
            return Seam(sum.toDouble() / n, max)
        }
    }

    /** Each intensity's seam against the canonical idle's: (state, intensity, seam, canonical). */
    private fun measure(
        state: Expression,
        intensities: List<Int>,
    ): List<String> {
        val canonical = seam(state, 5, null)
        val failures = mutableListOf<String>()
        for (i in intensities) {
            val s = seam(state, i, profileText)
            println("${state.wire} $i: seam $s, canonical $canonical")
            // A seam held through the idle (the defect class): the mean.
            if (s.mean > canonical.mean + MEAN_SLACK) failures += "${state.wire} $i mean: $s vs $canonical"
            // A passing one: the largest frame, against the measured peaks (docs/CHARACTER_RIG.md §5.5).
            val peak = PEAKS[state to i] ?: (canonical.max + MAX_SLACK)
            if (s.max > peak) failures += "${state.wire} $i peak: $s vs $canonical, allowed $peak"
        }
        return failures
    }

    @Test
    fun `sleepy keeps its hood over its shoulders at every captured intensity`() {
        val failures = measure(Expression.SLEEPY, listOf(1, 2, 8, 10))
        assertTrue(failures.joinToString("\n"), failures.isEmpty())
    }

    @Test
    fun `no expression opens the hood and sleeve seam at its weakest or strongest`() {
        val failures = Expression.entries.flatMap { measure(it, listOf(1, 10)) }
        assertTrue(failures.joinToString("\n"), failures.isEmpty())
    }

    @Test
    fun `the probe sees the defect the sleepy head hold removes`() {
        // The profile as it was: sleepy's head took part in its posture emphasis.
        val unheld =
            profileText.replace("\"hold_canonical\": [\"head\", \"upperarm_r\"", "\"hold_canonical\": [\"upperarm_r\"")
        assertTrue("the edit applies", unheld != profileText)
        val canonical = seam(Expression.SLEEPY, 5, null)
        val defect = seam(Expression.SLEEPY, 10, unheld)
        assertTrue("$defect vs canonical $canonical", defect.mean >= canonical.mean + DEFECT_MEAN)
    }

    companion object {
        private const val FRAME = 1f / 60f
        private const val SETTLE_FRAMES = 90
        private const val SAMPLE_EVERY = 3

        /** What motion scaling alone adds (the sway carries the sleeve a little further). */
        private const val MEAN_SLACK = 2.0
        private const val MAX_SLACK = 4

        /**
         * Measured passing peaks above canonical's (pixels at this probe's size), so they cannot grow
         * unnoticed: satisfied at 10 shows a short straight stretch of sleeve border over the fur
         * for about half a second at the top of its loop (§5.5, an observation for calibration).
         */
        private val PEAKS: Map<Pair<Expression, Int>, Int> = mapOf((Expression.SATISFIED to 10) to 14)

        /** The defect: a seam held through the idle, several times canonical's passing one. */
        private const val DEFECT_MEAN = 5.0

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

/**
 * Counts, in a [SIZE]×[SIZE] rendering of one frame, the pixels where an arm's white sticker
 * border (arm_l / arm_r texels brighter than [WHITE]) lies over grey hood fur (head texels in
 * [FUR]) — the seam. Rasterizes the frame's own triangles in draw order with nearest sampling.
 */
private class SeamProbe(
    png: ByteArray,
    atlas: String,
) {
    private val image = Png.decode(png)
    private val width = image.width
    private val argb = image.argb
    private val regions = regions(atlas)
    private val head = regions.getValue("head")
    private val arms = listOf(regions.getValue("arm_l"), regions.getValue("arm_r"))
    private val fur = BooleanArray(SIZE * SIZE)
    private val over = BooleanArray(SIZE * SIZE)

    fun count(
        g: RigGeometry,
        info: RigInfo,
    ): Int {
        fur.fill(false)
        over.fill(false)
        val scale = FIT * SIZE / maxOf(info.maxX - info.minX, info.maxY - info.minY)
        val cx = (info.minX + info.maxX) / 2f
        val cy = (info.minY + info.maxY) / 2f
        val sx = FloatArray(3)
        val sy = FloatArray(3)
        val u = FloatArray(3)
        val v = FloatArray(3)
        for (t in 0 until g.indexCount / 3) {
            for (k in 0 until 3) {
                val i = g.indices[t * 3 + k].toInt() and 0xFFFF
                sx[k] = (g.positions[2 * i] - cx) * scale + SIZE / 2f
                sy[k] = SIZE / 2f - (g.positions[2 * i + 1] - cy) * scale
                u[k] = g.texs[2 * i]
                v[k] = g.texs[2 * i + 1]
            }
            fill(sx, sy, u, v)
        }
        var n = 0
        for (p in fur.indices) if (fur[p] && over[p]) n++
        return n
    }

    private fun fill(
        sx: FloatArray,
        sy: FloatArray,
        u: FloatArray,
        v: FloatArray,
    ) {
        val area = (sx[1] - sx[0]) * (sy[2] - sy[0]) - (sx[2] - sx[0]) * (sy[1] - sy[0])
        if (kotlin.math.abs(area) < 1e-9f) return
        val x0 = maxOf(0, kotlin.math.floor(minOf(sx[0], sx[1], sx[2])).toInt())
        val x1 = minOf(SIZE - 1, kotlin.math.ceil(maxOf(sx[0], sx[1], sx[2])).toInt())
        val y0 = maxOf(0, kotlin.math.floor(minOf(sy[0], sy[1], sy[2])).toInt())
        val y1 = minOf(SIZE - 1, kotlin.math.ceil(maxOf(sy[0], sy[1], sy[2])).toInt())
        for (py in y0..y1) {
            for (px in x0..x1) {
                val x = px + 0.5f
                val y = py + 0.5f
                val w0 = ((sx[1] - x) * (sy[2] - y) - (sx[2] - x) * (sy[1] - y)) / area
                val w1 = ((sx[2] - x) * (sy[0] - y) - (sx[0] - x) * (sy[2] - y)) / area
                val w2 = 1f - w0 - w1
                if (w0 < -EDGE || w1 < -EDGE || w2 < -EDGE) continue
                val tx = (w0 * u[0] + w1 * u[1] + w2 * u[2]).toInt()
                val ty = (w0 * v[0] + w1 * v[1] + w2 * v[2]).toInt()
                texel(px + py * SIZE, tx, ty)
            }
        }
    }

    private fun texel(
        p: Int,
        tx: Int,
        ty: Int,
    ) {
        if (tx !in 0 until width || ty !in 0 until image.height) return
        val c = argb[ty * width + tx]
        if ((c ushr 24) < OPAQUE) return
        val lum = (((c shr 16) and 0xFF) + ((c shr 8) and 0xFF) + (c and 0xFF)) / 3
        when {
            head.contains(tx, ty) -> {
                fur[p] = lum in FUR
                over[p] = false
            }
            arms.any { it.contains(tx, ty) } -> over[p] = lum > WHITE
            else -> over[p] = false
        }
    }

    private data class Region(
        val x: Int,
        val y: Int,
        val w: Int,
        val h: Int,
    ) {
        fun contains(
            tx: Int,
            ty: Int,
        ) = tx in x until x + w && ty in y until y + h
    }

    private companion object {
        const val SIZE = 480
        const val FIT = 0.9f
        const val EDGE = 1e-6f
        const val OPAQUE = 200
        const val WHITE = 230
        val FUR = 100..215

        /** `name` / `bounds: x, y, w, h` pairs of a libGDX-style atlas. */
        fun regions(atlas: String): Map<String, Region> {
            val out = mutableMapOf<String, Region>()
            var name = ""
            for (raw in atlas.lines()) {
                val line = raw.trim()
                when {
                    line.startsWith("bounds:") -> {
                        val b = line.removePrefix("bounds:").split(',').map { it.trim().toInt() }
                        out[name] = Region(b[0], b[1], b[2], b[3])
                    }
                    line.isNotEmpty() && ':' !in line && !line.endsWith(".png") -> name = line
                }
            }
            return out
        }
    }
}

/** A minimal PNG reader for the atlas page (8-bit RGB or RGBA, not interlaced): ARGB pixels. */
private object Png {
    class Image(
        val width: Int,
        val height: Int,
        val argb: IntArray,
    )

    private const val SIGNATURE = 8
    private const val RGB = 2
    private const val RGBA = 6

    fun decode(bytes: ByteArray): Image {
        val b = ByteBuffer.wrap(bytes)
        b.position(SIGNATURE)
        var width = 0
        var height = 0
        var type = 0
        val data = ByteArrayOutputStream()
        while (b.remaining() > 0) {
            val length = b.int
            val name = String(ByteArray(4).also { b.get(it) }, Charsets.US_ASCII)
            val body = ByteArray(length).also { b.get(it) }
            b.int // CRC
            when (name) {
                "IHDR" -> {
                    val h = ByteBuffer.wrap(body)
                    width = h.int
                    height = h.int
                    require(h.get().toInt() == 8) { "8-bit only" }
                    type = h.get().toInt()
                    require(type == RGB || type == RGBA) { "RGB or RGBA only" }
                    require(body[12].toInt() == 0) { "not interlaced" }
                }
                "IDAT" -> data.write(body)
                "IEND" -> break
            }
        }
        val bpp = if (type == RGBA) 4 else 3
        val stride = width * bpp
        val raw = ByteArray(height * (stride + 1))
        Inflater().run {
            setInput(data.toByteArray())
            var n = 0
            while (n < raw.size && !finished()) n += inflate(raw, n, raw.size - n)
            end()
        }
        val prev = IntArray(stride)
        val line = IntArray(stride)
        val argb = IntArray(width * height)
        for (y in 0 until height) {
            val filter = raw[y * (stride + 1)].toInt()
            for (x in 0 until stride) {
                val v = raw[y * (stride + 1) + 1 + x].toInt() and 0xFF
                val a = if (x >= bpp) line[x - bpp] else 0
                val up = prev[x]
                val c = if (x >= bpp) prev[x - bpp] else 0
                line[x] = (v + predict(filter, a, up, c)) and 0xFF
            }
            for (x in 0 until width) {
                val o = x * bpp
                val alpha = if (bpp == 4) line[o + 3] else 0xFF
                argb[y * width + x] = (alpha shl 24) or (line[o] shl 16) or (line[o + 1] shl 8) or line[o + 2]
            }
            line.copyInto(prev)
        }
        return Image(width, height, argb)
    }

    private fun predict(
        filter: Int,
        a: Int,
        b: Int,
        c: Int,
    ): Int =
        when (filter) {
            0 -> 0
            1 -> a
            2 -> b
            3 -> (a + b) / 2
            else -> {
                val p = a + b - c
                val pa = kotlin.math.abs(p - a)
                val pb = kotlin.math.abs(p - b)
                val pc = kotlin.math.abs(p - c)
                when {
                    pa <= pb && pa <= pc -> a
                    pb <= pc -> b
                    else -> c
                }
            }
        }
}
