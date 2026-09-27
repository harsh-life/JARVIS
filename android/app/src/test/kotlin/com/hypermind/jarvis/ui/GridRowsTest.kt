package com.hypermind.jarvis.ui

import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.GridState
import com.hypermind.jarvis.contract.GridToggle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The grid screen follows the server's classification: "not classified" is never permission. */
class GridRowsTest {
    private val apps = listOf("com.example.notes" to "Notes", "com.wallet.pay" to "Wallet", "com.chat" to "Chat")
    private val policy = AppPolicy(nonSensitive = setOf("com.example.notes"), payment = setOf("com.wallet.pay"))

    private fun row(
        rows: List<GridRow>,
        pkg: String,
    ) = rows.single { it.packageName == pkg }

    private fun cell(
        row: GridRow,
        toggle: GridToggle,
    ) = row.cells.single { it.toggle == toggle }

    @Test
    fun `ui control is offered only in classified apps, screenshots only in non-sensitive ones`() {
        val rows = GridRows.of(apps, GridState(), policy)
        val chat = row(rows, "com.chat")
        assertFalse(cell(chat, GridToggle.UI_INTERACTION).enabled)
        assertNotNull(cell(chat, GridToggle.UI_INTERACTION).note)
        assertTrue(cell(chat, GridToggle.SCREEN_READ).enabled)
        val wallet = row(rows, "com.wallet.pay")
        assertTrue(cell(wallet, GridToggle.UI_INTERACTION).enabled)
        assertTrue(cell(wallet, GridToggle.UI_INTERACTION).note!!.contains("payment"))
        assertFalse(cell(wallet, GridToggle.SCREENSHOT).enabled)
        assertTrue(cell(row(rows, "com.example.notes"), GridToggle.SCREENSHOT).enabled)
    }

    @Test
    fun `a stored toggle the classification no longer allows is shown off`() {
        val grid = GridState(packages = mapOf("com.chat" to setOf(GridToggle.UI_INTERACTION, GridToggle.SCREEN_READ)))
        val chat = row(GridRows.of(apps, grid, policy), "com.chat")
        assertFalse(cell(chat, GridToggle.UI_INTERACTION).on)
        assertTrue(cell(chat, GridToggle.SCREEN_READ).on)
        // With no classification cached at all, nothing UI-acting is on offer.
        val none = GridRows.of(apps, grid, null)
        assertTrue(
            none.all { !cell(it, GridToggle.UI_INTERACTION).enabled && !cell(it, GridToggle.SCREENSHOT).enabled },
        )
    }

    @Test
    fun `apps with something on come first, and uninstalled grid entries still show so they can be turned off`() {
        val grid = GridState(packages = mapOf("com.gone" to setOf(GridToggle.SCREEN_READ)))
        val rows = GridRows.of(apps, grid, policy)
        assertEquals("com.gone", rows.first().packageName)
        assertEquals("com.gone", rows.first().label)
    }
}
