package com.hypermind.jarvis.presentation.ui

import android.provider.Settings
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
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
import com.hypermind.jarvis.presentation.character.CharacterTransitions
import com.hypermind.jarvis.presentation.character.Expression
import com.hypermind.jarvis.presentation.character.IntensityPolicy
import com.hypermind.jarvis.presentation.character.rig.AnimatedCharacter
import com.hypermind.jarvis.presentation.character.rig.RigAssets
import com.hypermind.jarvis.presentation.character.rig.RigLoader
import com.hypermind.jarvis.presentation.character.rig.RigNative
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The expressive character (OD-CHAR-1/2): the renderer only. It takes an
 * [Expression] and its intensity (both decided by `CharacterController`), a profile
 * and settings — and nothing else: no task, status, error or content can
 * reach it, so it cannot report on the system, and the status indicator next
 * to it remains the only status. It is decorative to accessibility services
 * for the same reason: a screen reader hears the truthful status line, not a
 * face.
 *
 * It draws the animated rig ([AnimatedCharacter], `character/rig/`) when that loads: each state
 * its own transition and idle, the long-idle action after ten minutes of one state. Without the
 * rig — no native runtime for this device, missing or invalid files — it draws the seven static
 * images below, which are also what shows while the rig loads. Either way it only shows the
 * expression it is given. The intensity changes how strongly the rig presents it (posture and idle
 * motion, docs/CHARACTER_RIG.md §5); the static images and reduced motion ignore it.
 *
 * The slot keeps one size whatever the image, so a change of expression never
 * moves the layout; the previous image stays until the next has decoded, and
 * a change cross-fades once for the configured duration — interrupted and
 * retargeted if a newer state arrives mid-fade — or swaps instantly when
 * transitions are off or motion is reduced (no repeating animation, no
 * drawing over the artwork). An image that fails to
 * load shows the profile's fallback; if that fails too, the slot stays empty.
 */
@Composable
fun Character(
    expression: Expression,
    profile: CharacterProfile,
    settings: CharacterSettings,
    modifier: Modifier = Modifier,
    height: Dp = characterHeight(),
    rig: RigLoader? = RigAssets.Bundled,
    intensity: Int = IntensityPolicy.NEUTRAL,
) {
    if (!settings.visible) return
    val context = LocalContext.current
    val systemReducesMotion =
        remember(context) {
            Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
        }
    // The bundled rig needs the native runtime; without it (an ABI it was not built for, a build
    // without it, a JVM test) the images show at once, with no loading step.
    val loader = rig?.takeIf { it !== RigAssets.Bundled || RigNative.available }
    val reducedMotion = settings.reducedMotion == CharacterSettings.ReducedMotion.ALWAYS || systemReducesMotion
    Box(modifier = modifier.size(height).testTag(TAG), contentAlignment = Alignment.Center) {
        if (loader != null) {
            AnimatedCharacter(expression, reducedMotion, loader, Modifier.fillMaxSize(), intensity) {
                StaticCharacter(expression, profile, settings, height, systemReducesMotion)
            }
        } else {
            StaticCharacter(expression, profile, settings, height, systemReducesMotion)
        }
    }
}

/** The seven approved images, one per state, cross-faded: the renderer before the rig, and its fallback. */
@Composable
private fun StaticCharacter(
    expression: Expression,
    profile: CharacterProfile,
    settings: CharacterSettings,
    height: Dp,
    systemReducesMotion: Boolean,
) {
    val assets = LocalContext.current.assets
    val targetPx = with(LocalDensity.current) { height.roundToPx() }
    var image by remember { mutableStateOf<CharacterImage?>(null) }
    LaunchedEffect(expression, profile, targetPx) {
        // The previous image stays on screen until this resolves (no blank frame).
        image =
            withContext(Dispatchers.IO) {
                CharacterAssets.load(profile, expression, targetPx) { path -> assets.open(path) }
            }
    }
    val fade = CharacterTransitions.fadeMillis(settings, systemReducesMotion).toInt()
    val animation: FiniteAnimationSpec<Float> = if (fade > 0) tween(fade) else snap()
    Crossfade(targetState = image, animationSpec = animation, label = "character", modifier = Modifier.fillMaxSize()) {
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
                        // Seven players for a preview grid is not worth it: the images.
                        rig = null,
                    )
                    val label = if (expression == selected) "• ${expression.wire}" else expression.wire
                    Text(label, style = MaterialTheme.typography.labelSmall)
                }
            }
        }
    }
}

/**
 * Development preview of intensity (debug builds only, beside [CharacterGallery]): pins the shown
 * expression at one of [choices]; "Compare with 5" toggles between the chosen intensity and the
 * canonical 5, so posture and idle motion can be judged against the authored animation on the
 * live character.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CharacterIntensityPicker(
    selected: Int,
    onSelect: (Int) -> Unit,
    choices: List<Int> = PREVIEW_INTENSITIES,
) {
    var chosen by remember { mutableStateOf(selected) }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Intensity (debug build): $selected", style = MaterialTheme.typography.titleSmall)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            for (i in choices) {
                val label = if (i == selected) "• $i" else "$i"
                Text(
                    label,
                    style = MaterialTheme.typography.labelLarge,
                    modifier =
                        Modifier
                            .testTag("$INTENSITY_TAG/$i")
                            .clickable(role = Role.Button) {
                                chosen = i
                                onSelect(i)
                            }.padding(8.dp),
                )
            }
        }
        val other = if (selected == IntensityPolicy.NEUTRAL) chosen else IntensityPolicy.NEUTRAL
        Text(
            if (selected == IntensityPolicy.NEUTRAL) "Compare: show $chosen" else "Compare with 5",
            style = MaterialTheme.typography.labelLarge,
            modifier =
                Modifier
                    .testTag("$INTENSITY_TAG/compare")
                    .clickable(role = Role.Button) { onSelect(other) }
                    .padding(8.dp),
        )
    }
}

/** The intensities the debug preview offers: every band, and 5 (canonical) to compare against. */
val PREVIEW_INTENSITIES = listOf(1, 2, 3, 4, 5, 7, 10)

const val CHARACTER_TAG = "character"
private const val TAG = CHARACTER_TAG
private const val GALLERY_TAG = "character-gallery"
private const val INTENSITY_TAG = "character-intensity"
private val GALLERY_HEIGHT = 72.dp
