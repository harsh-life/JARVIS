package com.hypermind.jarvis.permissions

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.GridToggle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class GridStoreTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val grid = GridStore(context.getSharedPreferences("grid-${System.nanoTime()}", Context.MODE_PRIVATE))

    @Test
    fun `every toggle starts off`() {
        val state = grid.state()
        assertTrue(state.packages.isEmpty())
        assertFalse(state.deviceState)
    }

    @Test
    fun `toggles are per app and turning one off is immediate`() {
        grid.set("com.example.notes", GridToggle.UI_INTERACTION, on = true)
        grid.set("com.example.notes", GridToggle.SCREEN_READ, on = true)
        assertEquals(
            setOf(GridToggle.UI_INTERACTION, GridToggle.SCREEN_READ),
            grid.state().packages["com.example.notes"],
        )
        assertNull(grid.state().packages["com.bank.app"])
        grid.set("com.example.notes", GridToggle.UI_INTERACTION, on = false)
        assertEquals(setOf(GridToggle.SCREEN_READ), grid.state().packages["com.example.notes"])
    }

    @Test
    fun `wipe returns everything to off`() {
        grid.set("com.example.notes", GridToggle.SCREENSHOT, on = true)
        grid.setDeviceState(true)
        grid.wipe()
        assertTrue(grid.state().packages.isEmpty())
        assertFalse(grid.state().deviceState)
    }

    @Test
    fun `the cached app policy is absent until fetched and wiped on revocation`() {
        val store = AppPolicyStore(context.getSharedPreferences("policy-${System.nanoTime()}", Context.MODE_PRIVATE))
        assertNull(store.current())
        store.update(AppPolicy(nonSensitive = setOf("com.example.notes")))
        assertEquals(setOf("com.example.notes"), store.current()?.nonSensitive)
        store.wipe()
        assertNull(store.current())
    }
}
