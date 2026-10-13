package com.hypermind.jarvis.presentation.ui

import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHeightIsEqualTo
import androidx.compose.ui.test.assertWidthIsEqualTo
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.presentation.character.CharacterProfile
import com.hypermind.jarvis.presentation.character.CharacterSettings
import com.hypermind.jarvis.presentation.character.Expression
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * OD-CHAR-1/2: the renderer — every state, fallback, interrupted fades,
 * reduced motion, sizing and the preview. The renderer shows whatever
 * expression it is given (the controller decides; CharacterControllerTest).
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(qualifiers = "w411dp-h891dp")
class CharacterViewTest {
    @get:Rule
    val compose = createComposeRule()

    private val profile = CharacterProfile.BUILT_IN
    private val expression = MutableStateFlow(Expression.DEFAULT)

    private fun show(settings: CharacterSettings = CharacterSettings(transition = CharacterSettings.Transition.NONE)) {
        compose.setContent {
            val shown by expression.collectAsState()
            Character(expression = shown, profile = profile, settings = settings)
        }
    }

    // Unmerged: the preview's clickable cells merge their children's semantics.
    private fun awaitShown(asset: String) {
        compose.waitUntil(TIMEOUT) {
            compose
                .onAllNodesWithTag(
                    "$CHARACTER_TAG/$asset",
                    useUnmergedTree = true,
                ).fetchSemanticsNodes()
                .isNotEmpty()
        }
        compose.onNodeWithTag("$CHARACTER_TAG/$asset", useUnmergedTree = true).assertExists()
    }

    @Test
    fun `the default character shows first`() {
        show()
        awaitShown("default.png")
    }

    @Test
    fun `each selected state shows its own image`() {
        show()
        awaitShown("default.png")
        for (state in Expression.entries.reversed()) {
            expression.value = state
            awaitShown(profile.assetFor(state))
        }
    }

    @Test
    fun `an unknown state shows the default image`() {
        show()
        expression.value = Expression.ANGRY
        awaitShown("angry.webp")
        expression.value = Expression.parse("furious")
        awaitShown("default.png")
    }

    @Test
    fun `the cross-fade setting renders the same images`() {
        show(CharacterSettings())
        awaitShown("default.png")
        expression.value = Expression.SLEEPY
        awaitShown("sleepy.webp")
    }

    @Test
    fun `transitions between every pair of states end on the target`() {
        show(CharacterSettings())
        awaitShown("default.png")
        for (from in Expression.entries) {
            for (to in Expression.entries) {
                if (from == to) continue
                expression.value = from
                awaitShown(profile.assetFor(from))
                expression.value = to
                awaitShown(profile.assetFor(to))
            }
        }
    }

    @Test
    fun `an interrupted cross-fade settles on the newest state only`() {
        show(CharacterSettings(transitionMillis = 2_000))
        awaitShown("default.png")
        expression.value = Expression.HAPPY
        expression.value = Expression.SAD
        expression.value = Expression.SURPRISED
        awaitShown("surprised.webp")
        compose.waitForIdle()
        for (gone in listOf("happy.webp", "sad.webp", "default.png")) {
            compose.onNodeWithTag("$CHARACTER_TAG/$gone", useUnmergedTree = true).assertDoesNotExist()
        }
    }

    @Test
    fun `with reduced motion the image swaps without a fade`() {
        show(CharacterSettings(reducedMotion = CharacterSettings.ReducedMotion.ALWAYS, transitionMillis = 2_000))
        awaitShown("default.png")
        compose.mainClock.autoAdvance = false
        expression.value = Expression.HAPPY
        // Decoding runs off the main thread; then a single frame swaps the image.
        compose.waitUntil(TIMEOUT) {
            compose.mainClock.advanceTimeByFrame()
            compose
                .onAllNodesWithTag(
                    "$CHARACTER_TAG/happy.webp",
                    useUnmergedTree = true,
                ).fetchSemanticsNodes()
                .isNotEmpty()
        }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("$CHARACTER_TAG/default.png", useUnmergedTree = true).assertDoesNotExist()
        compose.mainClock.autoAdvance = true
    }

    @Test
    fun `a hidden character renders nothing`() {
        show(CharacterSettings(visible = false))
        compose.waitForIdle()
        compose.onNodeWithTag(CHARACTER_TAG).assertDoesNotExist()
    }

    @Test
    fun `the character is decorative to screen readers - the status line carries the status`() {
        show()
        expression.value = Expression.SAD
        awaitShown("sad.webp")
        compose
            .onNodeWithTag("$CHARACTER_TAG/sad.webp")
            .assert(SemanticsMatcher.keyNotDefined(SemanticsProperties.ContentDescription))
        compose
            .onNodeWithTag(CHARACTER_TAG)
            .assert(SemanticsMatcher.keyNotDefined(SemanticsProperties.ContentDescription))
    }

    @Test
    fun `on a typical phone the slot is a share of the width`() {
        show()
        compose.onNodeWithTag(CHARACTER_TAG).assertHeightIsEqualTo(172.62.dp).assertWidthIsEqualTo(172.62.dp)
    }

    @Test
    @Config(qualifiers = "w320dp-h568dp")
    fun `on a small phone it shrinks`() {
        show()
        compose.onNodeWithTag(CHARACTER_TAG).assertHeightIsEqualTo(134.4.dp)
    }

    @Test
    @Config(qualifiers = "w891dp-h411dp")
    fun `in landscape it stays within a quarter of the height`() {
        show()
        compose.onNodeWithTag(CHARACTER_TAG).assertHeightIsEqualTo(98.64.dp)
    }

    @Test
    @Config(qualifiers = "w800dp-h1280dp")
    fun `on a tablet it is capped`() {
        show()
        compose.onNodeWithTag(CHARACTER_TAG).assertHeightIsEqualTo(220.dp)
    }

    @Test
    fun `the development preview shows all seven states and selects one`() {
        compose.setContent {
            val shown by expression.collectAsState()
            CharacterGallery(profile = profile, selected = shown, onSelect = { expression.value = it })
        }
        for (state in Expression.entries) {
            compose.onNodeWithTag("character-gallery/${state.wire}").assertExists()
            awaitShown(profile.assetFor(state))
        }
        compose.onNodeWithTag("character-gallery/surprised").performClick()
        assertEquals(Expression.SURPRISED, expression.value)
    }

    private companion object {
        const val TIMEOUT = 10_000L
    }
}
