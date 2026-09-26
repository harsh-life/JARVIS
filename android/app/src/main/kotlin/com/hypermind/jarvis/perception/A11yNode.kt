package com.hypermind.jarvis.perception

/**
 * The part of an Accessibility node this client reads — an interface so the
 * extraction, redaction and selector logic run as plain JVM tests, and so the
 * only code touching `AccessibilityNodeInfo` is the thin adapter in
 * [JarvisAccessibilityService].
 */
interface A11yNode {
    val className: String?
    val text: String?
    val contentDescription: String?
    val viewId: String?

    /** `[left, top, right, bottom]` in screen coordinates. */
    val bounds: List<Int>
    val clickable: Boolean
    val editable: Boolean
    val scrollable: Boolean
    val enabled: Boolean
    val checkable: Boolean
    val checked: Boolean
    val selected: Boolean
    val password: Boolean
    val visible: Boolean
    val childCount: Int

    fun child(index: Int): A11yNode?
}

/** What is in front: the app, and its window's Accessibility root. */
data class ForegroundWindow(
    val packageName: String,
    val activity: String?,
    val windowTitle: String?,
    val root: A11yNode?,
)

/** The live screen, as the Accessibility service sees it. */
interface ScreenSource {
    /** Whether the Accessibility service is connected right now. */
    val available: Boolean

    fun foreground(): ForegroundWindow?
}

/**
 * On-device OCR of the current screen (docs/23 §6 level 3). The frame is
 * captured, read and released inside [readScreen]; only text comes back, and
 * the image never leaves the device or outlives the call. `null` means the
 * rung is unavailable (old platform, secure window, engine failure).
 */
interface ScreenOcr {
    suspend fun readScreen(): List<OcrText>?
}

data class OcrText(
    val text: String,
    val bounds: List<Int>,
    val confidence: Double? = null,
)
