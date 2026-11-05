package com.hypermind.jarvis.presentation.character.rig

import android.graphics.Bitmap
import android.os.Build
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import com.hypermind.jarvis.presentation.character.Expression
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import android.graphics.Canvas as AndroidCanvas

const val RIG_TAG = "character/rig"

private sealed interface RigState {
    data object Loading : RigState

    data object Failed : RigState

    class Ready(
        val rig: LoadedRig,
    ) : RigState
}

/**
 * The animated character in the slot it is given: it shows [expression] — decided elsewhere — with
 * the rig's transitions and idles. Until the rig has loaded, and if it cannot (no native runtime,
 * missing or invalid files), [fallback] (the static images) is shown instead.
 *
 * - Frames run only while the screen is resumed: in the background time stands still (the player is
 *   paused) and the long-idle count stops; on return nothing jumps.
 * - The long-idle action is triggered here, after [LongIdleSchedule]'s ten minutes of one state.
 * - Reduced motion: the state's idle pose, still, redrawn only when the state changes.
 * - Leaving the composition releases the native rig and its page.
 */
@Composable
fun AnimatedCharacter(
    expression: Expression,
    reducedMotion: Boolean,
    rig: RigLoader,
    modifier: Modifier = Modifier,
    fallback: @Composable () -> Unit,
) {
    val assets = LocalContext.current.assets
    var state by remember(rig, reducedMotion) { mutableStateOf<RigState>(RigState.Loading) }
    LaunchedEffect(rig, reducedMotion) {
        // Not cancellable: a rig loaded after the screen went away is released, not leaked.
        val loaded =
            withContext(Dispatchers.IO + NonCancellable) {
                runCatching { rig.load({ assets.open(it) }, reducedMotion) }.getOrNull()
            }
        if (!isActive) {
            loaded?.close()
        } else {
            state = loaded?.let { RigState.Ready(it) } ?: RigState.Failed
        }
    }
    when (val s = state) {
        is RigState.Ready -> RigCanvas(s.rig, expression, reducedMotion, modifier)
        else -> fallback()
    }
}

@Composable
private fun RigCanvas(
    loaded: LoadedRig,
    expression: Expression,
    reducedMotion: Boolean,
    modifier: Modifier,
) {
    val player = loaded.player
    val painter = remember(loaded) { RigPainter(loaded.page, loaded.info) }
    val geometry = remember(loaded) { RigGeometry(loaded.info) }
    val surface = remember(loaded) { SoftwareSurface() }
    var frame by remember(loaded) { mutableLongStateOf(0L) }
    DisposableEffect(loaded) {
        onDispose {
            surface.release()
            loaded.close()
        }
    }
    LaunchedEffect(loaded) {
        player.start(expression, entrance = true)
    }
    LaunchedEffect(loaded, expression) {
        player.show(expression)
        if (reducedMotion) {
            player.advance(0f)
            if (player.build(geometry)) frame++
        }
    }
    if (!reducedMotion) {
        val lifecycle = LocalLifecycleOwner.current.lifecycle
        LaunchedEffect(loaded, lifecycle) {
            val schedule = LongIdleSchedule()
            lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                var last = -1L
                player.resume()
                try {
                    while (isActive) {
                        withFrameNanos { now ->
                            val millis = now / NANOS_PER_MILLI
                            if (last < 0) schedule.resume(millis)
                            schedule.observe(player.expression, millis)
                            if (schedule.due(millis) && player.triggerLongIdle()) schedule.fired(millis)
                            player.advance(if (last < 0) 0f else (now - last) / NANOS_PER_SECOND)
                            last = now
                            if (player.build(geometry)) frame++
                        }
                    }
                } finally {
                    player.pause()
                    schedule.pause()
                }
            }
        }
    }
    Canvas(modifier = modifier.testTag(RIG_TAG)) {
        if (frame == 0L || player.hidden) return@Canvas
        val w = size.width
        val h = size.height
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            drawIntoCanvas { painter.draw(it.nativeCanvas, w, h, geometry) }
        } else {
            // Before API 29 a hardware-accelerated canvas skips drawVertices: draw in software
            // into a bitmap the size of the view (reused across frames), then show that.
            val image = surface.draw(w.toInt(), h.toInt()) { canvas -> painter.draw(canvas, w, h, geometry) }
            if (image != null) drawImage(image)
        }
    }
}

/** A software-rendered bitmap the size of the view, kept across frames and reallocated only on resize. */
private class SoftwareSurface {
    private var bitmap: Bitmap? = null
    private var image: ImageBitmap? = null
    private val canvas = AndroidCanvas()

    fun draw(
        width: Int,
        height: Int,
        paint: (AndroidCanvas) -> Unit,
    ): ImageBitmap? {
        if (width <= 0 || height <= 0) return null
        val current = bitmap
        val b =
            if (current != null && current.width == width && current.height == height) {
                current
            } else {
                current?.recycle()
                Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).also {
                    bitmap = it
                    image = it.asImageBitmap()
                    canvas.setBitmap(it)
                }
            }
        b.eraseColor(0)
        paint(canvas)
        return image
    }

    fun release() {
        canvas.setBitmap(null)
        bitmap?.recycle()
        bitmap = null
        image = null
    }
}

private const val NANOS_PER_MILLI = 1_000_000L
private const val NANOS_PER_SECOND = 1_000_000_000f
