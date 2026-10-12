package com.hypermind.jarvis.presentation.ui

import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assertHeightIsEqualTo
import androidx.compose.ui.test.assertWidthIsEqualTo
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.presentation.character.CharacterPresentation
import com.hypermind.jarvis.presentation.character.CharacterSettings
import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/** OD-CHAR-1: the character as rendered — selection, fallback, sizing and the preview. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(qualifiers = "w411dp-h891dp")
class CharacterViewTest {
    @get:Rule
    val compose = createComposeRule()

    private val presentation = CharacterPresentation()

    private fun show(settings: CharacterSettings = CharacterSettings(transition = CharacterSettings.Transition.NONE)) {
        compose.setContent {
            val expression by presentation.expression.collectAsState()
            Character(expression = expression, profile = presentation.profile, settings = settings)
        }
    }

    private fun awaitShown(asset: String) {
        compose.waitUntil(TIMEOUT) {
            compose.onAllNodesWithTag("$CHARACTER_TAG/$asset").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("$CHARACTER_TAG/$asset").assertExists()
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
            presentation.select(state)
            awaitShown(presentation.profile.assetFor(state))
        }
    }

    @Test
    fun `an unknown state shows the default image`() {
        show()
        presentation.select(Expression.ANGRY)
        awaitShown("angry.webp")
        presentation.select("furious")
        awaitShown("default.png")
    }

    @Test
    fun `the cross-fade setting renders the same images`() {
        show(CharacterSettings())
        awaitShown("default.png")
        presentation.select(Expression.SLEEPY)
        awaitShown("sleepy.webp")
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
        presentation.select(Expression.SAD)
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
            val expression by presentation.expression.collectAsState()
            CharacterGallery(profile = presentation.profile, selected = expression, onSelect = presentation::select)
        }
        for (state in Expression.entries) {
            compose.onNodeWithTag("character-gallery/${state.wire}").assertExists()
            awaitShown(presentation.profile.assetFor(state))
        }
        compose.onNodeWithTag("character-gallery/surprised").performClick()
        assertEquals(Expression.SURPRISED, presentation.expression.value)
    }

    private companion object {
        const val TIMEOUT = 10_000L
    }
}
