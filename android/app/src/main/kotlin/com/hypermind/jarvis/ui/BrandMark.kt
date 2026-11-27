package com.hypermind.jarvis.ui

import android.content.res.Resources
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException

/**
 * JARVIS's brand mark: the satisfied face, the owner's original artwork
 * (`assets/character/default/satisfied.png`), framed as a launcher shows the
 * app icon (the `brand_*` resources that renderBrandIcons generates in
 * app/build.gradle.kts beside the icons), on the launcher icon's navy disc.
 *
 * It is the logo, not the character: it never changes with the character's
 * expression and reads nothing the character reads. The character portrait
 * below it is still where expressions are shown.
 */
@Composable
fun BrandHeader(modifier: Modifier = Modifier) {
    Row(
        modifier = modifier.semantics(mergeDescendants = true) { heading() },
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        BrandBadge(size = 40.dp)
        Text("JARVIS", style = MaterialTheme.typography.headlineSmall)
    }
}

/** The framed satisfied face on its disc; just the disc if the artwork cannot be read. */
@Composable
fun BrandBadge(
    size: Dp,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val assets = context.assets
    val asset = remember { context.getString(R.string.brand_asset) }
    val source = remember { BrandFrame.source(context.resources) }
    val targetPx = with(LocalDensity.current) { size.roundToPx() }
    var bitmap by remember { mutableStateOf<Bitmap?>(null) }
    LaunchedEffect(targetPx) {
        bitmap =
            withContext(Dispatchers.IO) {
                try {
                    val sample = source.sampleSize(targetPx)
                    val options = BitmapFactory.Options().apply { inSampleSize = sample }
                    assets.open(asset).use { BitmapFactory.decodeStream(it, null, options) }
                } catch (ignored: IOException) {
                    null
                }
            }
    }
    Canvas(modifier = modifier.size(size).clip(CircleShape)) {
        drawRect(BrandBackground)
        val image = bitmap ?: return@Canvas
        val sample = source.sampleSize(targetPx)
        val frame = source.scaledBy(sample)
        val part = frame.visiblePart(image.width, image.height, this.size.width) ?: return@Canvas
        drawImage(
            image.asImageBitmap(),
            srcOffset = IntOffset(part.srcLeft, part.srcTop),
            srcSize = IntSize(part.srcWidth, part.srcHeight),
            dstOffset = IntOffset(part.dstLeft, part.dstTop),
            dstSize = IntSize(part.dstWidth, part.dstHeight),
            filterQuality = FilterQuality.High,
        )
    }
}

/** res/values/colors.xml `ic_launcher_background`: the launcher icon's own background. */
private val BrandBackground = Color(0xFF0E1424)

/**
 * A square window onto the artwork, in its pixels. It may run past the image's
 * edges (the surroundings are transparent there), so drawing it takes the part
 * that overlaps the image and places that part where it falls in the square.
 */
data class BrandFrame(
    val left: Int,
    val top: Int,
    val side: Int,
) {
    /** The largest power-of-two decode reduction that keeps the frame at least [targetPx] across. */
    fun sampleSize(targetPx: Int): Int {
        var sample = 1
        while (targetPx > 0 && side / (sample * 2) >= targetPx) sample *= 2
        return sample
    }

    fun scaledBy(sample: Int): BrandFrame =
        BrandFrame(Math.floorDiv(left, sample), Math.floorDiv(top, sample), side / sample)

    /** The overlap of this frame with a [width]×[height] image, mapped into a [dstSide]-px square. */
    fun visiblePart(
        width: Int,
        height: Int,
        dstSide: Float,
    ): Part? {
        val srcLeft = left.coerceAtLeast(0)
        val srcTop = top.coerceAtLeast(0)
        val srcRight = (left + side).coerceAtMost(width)
        val srcBottom = (top + side).coerceAtMost(height)
        if (srcRight <= srcLeft || srcBottom <= srcTop || side <= 0) return null
        val k = dstSide / side
        val dstLeft = Math.round((srcLeft - left) * k)
        val dstTop = Math.round((srcTop - top) * k)
        return Part(
            srcLeft = srcLeft,
            srcTop = srcTop,
            srcWidth = srcRight - srcLeft,
            srcHeight = srcBottom - srcTop,
            dstLeft = dstLeft,
            dstTop = dstTop,
            dstWidth = Math.round((srcRight - left) * k) - dstLeft,
            dstHeight = Math.round((srcBottom - top) * k) - dstTop,
        )
    }

    data class Part(
        val srcLeft: Int,
        val srcTop: Int,
        val srcWidth: Int,
        val srcHeight: Int,
        val dstLeft: Int,
        val dstTop: Int,
        val dstWidth: Int,
        val dstHeight: Int,
    )

    companion object {
        /** The badge's frame on the artwork, from the resources renderBrandIcons generates. */
        fun source(res: Resources): BrandFrame =
            BrandFrame(
                res.getInteger(R.integer.brand_frame_left),
                res.getInteger(R.integer.brand_frame_top),
                res.getInteger(R.integer.brand_frame_side),
            )
    }
}
