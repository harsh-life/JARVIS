package com.hypermind.jarvis.presentation.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hypermind.jarvis.presentation.PresentationSignal
import com.hypermind.jarvis.presentation.PresentationSignal.Glyph
import com.hypermind.jarvis.presentation.PresentationSignal.Motion
import com.hypermind.jarvis.presentation.PresentationSignal.Tone
import com.hypermind.jarvis.ui.theme.ErrorRed
import com.hypermind.jarvis.ui.theme.Primary
import com.hypermind.jarvis.ui.theme.PrimaryContainer
import com.hypermind.jarvis.ui.theme.Secondary

/**
 * The plain status indicator (docs/23 §7): a ring whose colour, motion and
 * centre glyph all carry the state, so it reads without relying on colour.
 * It takes a [PresentationSignal] and nothing else — no task content can
 * reach it. This is the one composable the future character replaces.
 */
@Composable
fun StatusIndicator(
    signal: PresentationSignal,
    label: String,
    modifier: Modifier = Modifier,
    size: Dp = 48.dp,
) {
    val color = tone(signal.tone)
    val transition = rememberInfiniteTransition(label = "status")
    val spin by transition.animateFloat(
        0f,
        360f,
        infiniteRepeatable(tween(1_400, easing = LinearEasing), RepeatMode.Restart),
        label = "spin",
    )
    val pulse by transition.animateFloat(
        0.55f,
        1f,
        infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "pulse",
    )
    val alpha =
        when (signal.motion) {
            Motion.BREATHING, Motion.PULSING -> pulse
            else -> 1f
        }
    Box(
        modifier = modifier.size(size).semantics { contentDescription = label },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(modifier = Modifier.size(size)) {
            val stroke = Stroke(width = this.size.minDimension * STROKE)
            val inset = stroke.width / 2
            val arcSize =
                androidx.compose.ui.geometry.Size(
                    this.size.width - stroke.width,
                    this.size.height - stroke.width,
                )
            val topLeft =
                androidx.compose.ui.geometry
                    .Offset(inset, inset)
            if (signal.motion == Motion.WORKING) {
                drawArc(color.copy(alpha = 0.25f), 0f, 360f, false, topLeft, arcSize, style = stroke)
                drawArc(color, spin, 100f, false, topLeft, arcSize, style = stroke)
            } else {
                val width = if (signal.emphasis) stroke.width * 1.6f else stroke.width
                drawArc(color.copy(alpha = alpha), 0f, 360f, false, topLeft, arcSize, style = Stroke(width))
            }
        }
        Text(
            glyph(signal.glyph),
            color = color,
            fontSize = (size.value * 0.36f).sp,
            fontWeight = FontWeight.Bold,
        )
    }
}

private const val STROKE = 0.09f
private val Calm = Color(0xFF9AA0AE)

private fun tone(tone: Tone): Color =
    when (tone) {
        Tone.CALM -> Calm
        Tone.ACTIVE -> Primary
        Tone.ATTENTION -> PrimaryContainer
        Tone.SUCCESS -> Secondary
        Tone.PROBLEM -> ErrorRed
        Tone.BLOCKED -> ErrorRed
    }

private fun glyph(glyph: Glyph): String =
    when (glyph) {
        Glyph.READY -> "•"
        Glyph.LISTENING -> "≈"
        Glyph.WORKING -> "…"
        Glyph.APPROVAL -> "?"
        Glyph.WAITING -> "‖"
        Glyph.DONE -> "✓"
        Glyph.PROBLEM -> "!"
        Glyph.STOPPED -> "×"
        Glyph.OFFLINE -> "○"
    }
