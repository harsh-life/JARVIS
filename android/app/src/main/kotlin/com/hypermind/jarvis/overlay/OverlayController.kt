package com.hypermind.jarvis.overlay

import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.ComposeView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.savedstate.SavedStateRegistry
import androidx.savedstate.SavedStateRegistryController
import androidx.savedstate.SavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import com.hypermind.jarvis.presentation.PresentationState
import com.hypermind.jarvis.ui.theme.JarvisTheme
import kotlin.math.abs

/** What the overlay can ask for. None of it can approve, grant or run anything. */
interface OverlayActions {
    fun openApp()

    fun cancelTask()

    fun hideOverlay()
}

/**
 * The floating status overlay (docs/23 §1), attached by the existing channel
 * foreground service — no second service, no second notification.
 *
 * Adapted from the donor overlay's safe choices, not its architecture:
 * * **State, not content.** It shows the [PresentationState] signal and its
 *   one-line wording — never the user's words, a result, a pending action or
 *   screen text — so nothing leaks to anyone looking over a shoulder.
 * * **Never steals input.** `FLAG_NOT_FOCUSABLE` + `FLAG_NOT_TOUCH_MODAL`: it
 *   has no text field and cannot take the keyboard or touches meant for the
 *   app underneath.
 * * **Never approves.** An action waiting for approval shows "Needs your
 *   approval" and opens the app, where the canonical confirmation card is —
 *   an approval tapped in a floating window could be tapjacked.
 * * **At most one.** [show] is idempotent; [hide] and [destroy] remove it.
 */
class OverlayController(
    private val context: Context,
    private val actions: OverlayActions,
    private val windowManager: WindowManager = context.getSystemService(WindowManager::class.java),
) : LifecycleOwner, SavedStateRegistryOwner {
    private val lifecycleRegistry = LifecycleRegistry(this)
    private val savedState = SavedStateRegistryController.create(this)
    private val state = mutableStateOf<PresentationState?>(null)
    private val expanded = mutableStateOf(false)
    private var view: ComposeView? = null

    override val lifecycle: Lifecycle get() = lifecycleRegistry
    override val savedStateRegistry: SavedStateRegistry get() = savedState.savedStateRegistry

    init {
        savedState.performRestore(null)
        lifecycleRegistry.currentState = Lifecycle.State.CREATED
    }

    val attached: Boolean get() = view != null

    /** Show [presentation] (attaching the window once), or remove the window when [visible] is false. */
    fun render(
        presentation: PresentationState,
        visible: Boolean,
    ) {
        state.value = presentation
        if (visible) show() else hide()
    }

    private fun show() {
        if (view != null) return
        val params = layoutParams()
        val compose =
            ComposeView(context).apply {
                setViewTreeLifecycleOwner(this@OverlayController)
                setViewTreeSavedStateRegistryOwner(this@OverlayController)
                setContent {
                    JarvisTheme {
                        state.value?.let {
                            OverlayContent(
                                state = it,
                                expanded = expanded.value,
                                onToggle = { expanded.value = !expanded.value },
                                actions = actions,
                            )
                        }
                    }
                }
            }
        compose.setOnTouchListener(
            DragToMove(params) { runCatching { windowManager.updateViewLayout(compose, params) } },
        )
        try {
            windowManager.addView(compose, params)
            view = compose
            lifecycleRegistry.currentState = Lifecycle.State.RESUMED
        } catch (ignored: WindowManager.BadTokenException) {
            view = null
        } catch (ignored: SecurityException) {
            // "Display over other apps" was withdrawn: simply not shown.
            view = null
        }
    }

    fun hide() {
        val current = view ?: return
        view = null
        expanded.value = false
        runCatching { windowManager.removeView(current) }
        lifecycleRegistry.currentState = Lifecycle.State.CREATED
    }

    fun destroy() {
        hide()
        lifecycleRegistry.currentState = Lifecycle.State.DESTROYED
    }

    private fun layoutParams() =
        WindowManager
            .LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                } else {
                    @Suppress("DEPRECATION")
                    WindowManager.LayoutParams.TYPE_PHONE
                },
                FLAGS,
                PixelFormat.TRANSLUCENT,
            ).apply {
                gravity = Gravity.TOP or Gravity.START
                x = START_X
                y = START_Y
            }

    /** Drag to move; a touch that did not move falls through to the content (tap). */
    private class DragToMove(
        private val params: WindowManager.LayoutParams,
        private val update: () -> Unit,
    ) : View.OnTouchListener {
        private var downX = 0f
        private var downY = 0f
        private var startX = 0
        private var startY = 0
        private var dragging = false

        override fun onTouch(
            v: View,
            event: MotionEvent,
        ): Boolean =
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX
                    downY = event.rawY
                    startX = params.x
                    startY = params.y
                    dragging = false
                    false
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - downX
                    val dy = event.rawY - downY
                    if (abs(dx) > SLOP || abs(dy) > SLOP) dragging = true
                    if (dragging) {
                        params.x = startX + dx.toInt()
                        params.y = startY + dy.toInt()
                        update()
                    }
                    dragging
                }
                else -> dragging
            }
    }

    companion object {
        /** Never focusable (no keyboard, no stolen input); touches outside go to the app underneath. */
        const val FLAGS =
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
        private const val START_X = 24
        private const val START_Y = 320
        private const val SLOP = 12f

        fun canDraw(context: Context): Boolean = Settings.canDrawOverlays(context)

        /** The system screen where the user grants "display over other apps" to JARVIS. */
        fun permissionIntent(context: Context): Intent =
            Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}
