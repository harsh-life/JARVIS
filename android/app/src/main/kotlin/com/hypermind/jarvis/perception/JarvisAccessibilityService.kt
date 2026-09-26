package com.hypermind.jarvis.perception

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.concurrent.Executor
import kotlin.coroutines.resume

/**
 * The Accessibility service (adapted from the donor's
 * `HypermindAccessibilityService`, docs/23 §2).
 *
 * Kept from the donor: it holds no logic and reads **on demand** — it
 * subscribes only to window-state changes (app switches), never to the
 * per-frame content-changed firehose that bogged the donor's test devices
 * down, and does no per-event work beyond remembering which activity and
 * title are in front.
 *
 * Not kept: the donor's tier ladder that sent a screenshot when the tree was
 * empty. Here a screenshot is never a fallback (docs/23 §6); the ladder is
 * [ScreenPerception], and a screenshot is its own operation.
 *
 * It decides nothing. Whether a read may happen was decided by the server and
 * re-checked by the device guard before anything calls into it.
 */
class JarvisAccessibilityService : AccessibilityService() {
    @Volatile
    private var lastWindow: Triple<String, String?, String?>? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        listeners.forEach { it() }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString() ?: return
        lastWindow = Triple(pkg, event.className?.toString(), event.text?.firstOrNull()?.toString())
    }

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        if (instance === this) instance = null
        listeners.forEach { it() }
        super.onDestroy()
    }

    fun foreground(): ForegroundWindow? {
        val root = rootInActiveWindow ?: return null
        val pkg = root.packageName?.toString() ?: return null
        // Activity and title only if the last window-state event was for the
        // same app; never another app's.
        val window = lastWindow?.takeIf { it.first == pkg }
        return ForegroundWindow(pkg, window?.second, window?.third, NodeInfoAdapter(root))
    }

    /**
     * A transient bitmap of the default display (API 30+), or null — also for
     * a FLAG_SECURE window, which the platform refuses to capture. The caller
     * must recycle it.
     */
    suspend fun capture(): Bitmap? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return null
        return suspendCancellableCoroutine { cont ->
            try {
                takeScreenshot(
                    Display.DEFAULT_DISPLAY,
                    Executor { it.run() },
                    object : TakeScreenshotCallback {
                        override fun onSuccess(result: ScreenshotResult) {
                            val buffer = result.hardwareBuffer
                            val bitmap =
                                try {
                                    Bitmap
                                        .wrapHardwareBuffer(
                                            buffer,
                                            result.colorSpace,
                                        )?.copy(Bitmap.Config.ARGB_8888, false)
                                } finally {
                                    buffer.close()
                                }
                            if (cont.isActive) cont.resume(bitmap) else bitmap?.recycle()
                        }

                        override fun onFailure(errorCode: Int) {
                            if (cont.isActive) cont.resume(null)
                        }
                    },
                )
            } catch (ignored: IllegalStateException) {
                if (cont.isActive) cont.resume(null)
            } catch (ignored: SecurityException) {
                if (cont.isActive) cont.resume(null)
            }
        }
    }

    companion object {
        @Volatile
        var instance: JarvisAccessibilityService? = null
            private set

        private val listeners = java.util.concurrent.CopyOnWriteArrayList<() -> Unit>()

        /** Called when the service connects or goes away (platform status). */
        fun onAvailabilityChanged(listener: () -> Unit) {
            listeners += listener
        }

        /** The [ScreenSource] backed by whichever instance is connected. */
        val screen: ScreenSource =
            object : ScreenSource {
                override val available: Boolean get() = instance != null

                override fun foreground(): ForegroundWindow? = instance?.foreground()
            }
    }
}

/** The thin adapter from the platform node to [A11yNode]. */
private class NodeInfoAdapter(
    private val info: AccessibilityNodeInfo,
) : A11yNode {
    override val className: String? get() = info.className?.toString()
    override val text: String? get() = info.text?.toString()
    override val contentDescription: String? get() = info.contentDescription?.toString()
    override val viewId: String? get() = info.viewIdResourceName
    override val bounds: List<Int>
        get() = Rect().also(info::getBoundsInScreen).let { listOf(it.left, it.top, it.right, it.bottom) }
    override val clickable: Boolean get() = info.isClickable
    override val editable: Boolean get() = info.isEditable
    override val scrollable: Boolean get() = info.isScrollable
    override val enabled: Boolean get() = info.isEnabled
    override val checkable: Boolean get() = info.isCheckable
    override val checked: Boolean get() = info.isChecked
    override val selected: Boolean get() = info.isSelected
    override val password: Boolean get() = info.isPassword
    override val visible: Boolean get() = info.isVisibleToUser
    override val childCount: Int get() = info.childCount

    override fun child(index: Int): A11yNode? = info.getChild(index)?.let(::NodeInfoAdapter)
}
