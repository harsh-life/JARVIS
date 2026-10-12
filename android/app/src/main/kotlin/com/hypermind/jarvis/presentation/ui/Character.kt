package com.hypermind.jarvis.presentation.ui

import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.FiniteAnimationSpec
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.presentation.character.CharacterAssets
import com.hypermind.jarvis.presentation.character.CharacterImage
import com.hypermind.jarvis.presentation.character.CharacterProfile
import com.hypermind.jarvis.presentation.character.CharacterSettings
import com.hypermind.jarvis.presentation.character.CharacterSizing
import com.hypermind.jarvis.presentation.character.Expression
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The expressive character (OD-CHAR-1). It takes an [Expression], a profile
 * and settings — and nothing else: no task, status, error or content can
 * reach it, so it cannot report on the system, and the status indicator next
 * to it remains the only status. It is decorative to accessibility services
 * for the same reason: a screen reader hears the truthful status line, not a
 * face.
 *
 * The slot keeps one size whatever the image, so a change of expression never
 * moves the layout; the previous image stays until the next has decoded, and
 * a change cross-fades once (no repeating animation). An image that fails to
 * load shows the profile's fallback; if that fails too, the slot stays empty.
 */
@Composable
fun Character(
    expression: Expression,
    profile: CharacterProfile,
    settings: CharacterSettings,
    modifier: Modifier = Modifier,
    height: Dp = characterHeight(),
) {
    if (!settings.visible) return
    val assets = LocalContext.current.assets
    val targetPx = with(LocalDensity.current) { height.roundToPx() }
    val image by produceState<CharacterImage?>(null, expression, profile, targetPx) {
        // The previous image stays on screen until this resolves (no blank frame).
        value =
            withContext(Dispatchers.IO) {
                CharacterAssets.load(profile, expression, targetPx) { path -> assets.open(path) }
            }
    }
    val animation: FiniteAnimationSpec<Float> =
        when (settings.transition) {
            CharacterSettings.Transition.CROSSFADE -> tween(FADE_MILLIS)
            CharacterSettings.Transition.NONE -> snap()
        }
    Box(modifier = modifier.size(height).testTag(TAG), contentAlignment = Alignment.Center) {
        Crossfade(targetState = image, animationSpec = animation, contentKey = { it?.asset }, label = "character") {
            if (it != null) {
                Image(
                    bitmap = it.bitmap.asImageBitmap(),
                    contentDescription = null,
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxSize().testTag("$TAG/${it.asset}"),
                )
            }
        }
    }
}

/** The character's height for the current window ([CharacterSizing]). */
@Composable
fun characterHeight(): Dp {
    val configuration = LocalConfiguration.current
    return CharacterSizing.heightDp(configuration.screenWidthDp, configuration.screenHeightDp).dp
}

/**
 * Development preview: every state of the profile side by side, each
 * selectable. Shown only in debug builds (MainActivity's Settings tab), so
 * all seven can be checked on a real device without any classifier.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CharacterGallery(
    profile: CharacterProfile,
    selected: Expression,
    onSelect: (Expression) -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Character preview (debug build)", style = MaterialTheme.typography.titleSmall)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Expression.entries.forEach { expression ->
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier =
                        Modifier
                            .testTag("$GALLERY_TAG/${expression.wire}")
                            .clickable(role = Role.Button) { onSelect(expression) },
                ) {
                    Character(
                        expression = expression,
                        profile = profile,
                        settings = CharacterSettings(transition = CharacterSettings.Transition.NONE),
                        height = GALLERY_HEIGHT,
                    )
                    val label = if (expression == selected) "• ${expression.wire}" else expression.wire
                    Text(label, style = MaterialTheme.typography.labelSmall)
                }
            }
        }
    }
}

const val CHARACTER_TAG = "character"
private const val TAG = CHARACTER_TAG
private const val GALLERY_TAG = "character-gallery"
private const val FADE_MILLIS = 220
private val GALLERY_HEIGHT = 72.dp
