package com.hypermind.jarvis.overlay

import android.content.Context
import android.view.WindowManager
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.presentation.ConnectionPhase
import com.hypermind.jarvis.presentation.DeviceContext
import com.hypermind.jarvis.presentation.PresentationState
import com.hypermind.jarvis.presentation.TaskPhase
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowWindowManagerImpl

/** The overlay's lifecycle and its limits (docs/23 §7). */
@RunWith(RobolectricTestRunner::class)
class OverlayTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val windowManager = context.getSystemService(WindowManager::class.java)
    private val state =
        PresentationState(TaskPhase.WAITING_FOR_CONFIRMATION, deviceContext = DeviceContext(ConnectionPhase.CONNECTED))
    private val actions =
        object : OverlayActions {
            override fun openApp() = Unit

            override fun cancelTask() = Unit

            override fun hideOverlay() = Unit
        }

    private fun views() = (shadowOf(windowManager) as ShadowWindowManagerImpl).views

    @Test
    fun `shown only when turned on, allowed, enrolled, and JARVIS itself is not in front`() {
        assertTrue(OverlayPolicy.visible(enabled = true, canDraw = true, enrolled = true, appInForeground = false))
        assertFalse(OverlayPolicy.visible(enabled = false, canDraw = true, enrolled = true, appInForeground = false))
        assertFalse(OverlayPolicy.visible(enabled = true, canDraw = false, enrolled = true, appInForeground = false))
        assertFalse(OverlayPolicy.visible(enabled = true, canDraw = true, enrolled = false, appInForeground = false))
        assertFalse(OverlayPolicy.visible(enabled = true, canDraw = true, enrolled = true, appInForeground = true))
    }

    @Test
    fun `off by default, and the choice sticks`() {
        val prefs = context.getSharedPreferences("overlay-${System.nanoTime()}", Context.MODE_PRIVATE)
        assertFalse(OverlaySettings(prefs).enabled.value)
        OverlaySettings(prefs).set(true)
        assertTrue(OverlaySettings(prefs).enabled.value)
    }

    @Test
    fun `at most one overlay window, removed on hide and on destroy`() {
        val before = views().size
        val overlay = OverlayController(context, actions, windowManager)
        repeat(3) { overlay.render(state, visible = true) }
        assertEquals(before + 1, views().size)
        assertTrue(overlay.attached)
        overlay.render(state, visible = false)
        assertEquals(before, views().size)
        overlay.render(state.copy(taskStatus = TaskPhase.EXECUTING), visible = true)
        assertEquals(before + 1, views().size)
        overlay.destroy()
        assertEquals(before, views().size)
        assertFalse(overlay.attached)
    }

    @Test
    fun `the window never takes focus or input meant for the app underneath`() {
        val flags = OverlayController.FLAGS
        assertTrue(flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE != 0)
        assertTrue(flags and WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL != 0)
        val overlay = OverlayController(context, actions, windowManager)
        overlay.render(state, visible = true)
        val params = views().last().layoutParams as WindowManager.LayoutParams
        assertTrue(params.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE != 0)
        assertEquals(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, params.type)
        overlay.destroy()
    }

    @Test
    fun `the overlay can open the app, cancel or hide — never approve or grant`() {
        val names =
            OverlayActions::class.java.declaredMethods
                .map { it.name }
                .toSet()
        assertEquals(setOf("openApp", "cancelTask", "hideOverlay"), names)
    }

    @Test
    fun `the foreground count never goes negative`() {
        repeat(3) { AppForeground.stopped() }
        assertEquals(0, AppForeground.visibleCount.value)
        AppForeground.started()
        assertEquals(1, AppForeground.visibleCount.value)
        AppForeground.stopped()
    }
}
