package com.hypermind.jarvis.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** "Remove this phone" never acts on the first tap: only the dialog's Remove does. */
@RunWith(RobolectricTestRunner::class)
class RemovePhoneButtonTest {
    @get:Rule
    val compose = createComposeRule()

    private var removals = 0

    private fun show() = compose.setContent { RemovePhoneButton { removals++ } }

    @Test
    fun `the first tap only asks`() {
        show()
        compose.onNodeWithText(REMOVE_BUTTON).performClick()
        compose.onNodeWithText(REMOVE_TITLE).assertIsDisplayed()
        compose.onNodeWithText(REMOVE_TEXT).assertIsDisplayed()
        assertEquals(0, removals)
    }

    @Test
    fun `cancel removes nothing and closes the dialog`() {
        show()
        compose.onNodeWithText(REMOVE_BUTTON).performClick()
        compose.onNodeWithText(REMOVE_CANCEL).performClick()
        compose.onNodeWithText(REMOVE_TITLE).assertDoesNotExist()
        assertEquals(0, removals)
    }

    @Test
    fun `remove in the dialog removes exactly once`() {
        show()
        compose.onNodeWithText(REMOVE_BUTTON).performClick()
        compose.onNodeWithText(REMOVE_CONFIRM).performClick()
        compose.onNodeWithText(REMOVE_TITLE).assertDoesNotExist()
        assertEquals(1, removals)
    }
}
