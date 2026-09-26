package com.hypermind.jarvis.perception

/** A plain in-memory [A11yNode] tree for JVM tests. */
data class FakeNode(
    override val className: String? = "android.widget.TextView",
    override val text: String? = null,
    override val contentDescription: String? = null,
    override val viewId: String? = null,
    override val bounds: List<Int> = listOf(0, 0, 100, 40),
    override val clickable: Boolean = false,
    override val editable: Boolean = false,
    override val scrollable: Boolean = false,
    override val enabled: Boolean = true,
    override val checkable: Boolean = false,
    override val checked: Boolean = false,
    override val selected: Boolean = false,
    override val password: Boolean = false,
    override val visible: Boolean = true,
    val children: List<FakeNode> = emptyList(),
) : A11yNode {
    override val childCount: Int get() = children.size

    override fun child(index: Int): A11yNode? = children.getOrNull(index)
}

fun layout(vararg children: FakeNode) = FakeNode(className = "android.widget.FrameLayout", children = children.toList())

class FakeScreen(
    var window: ForegroundWindow?,
) : ScreenSource {
    override val available: Boolean get() = true

    override fun foreground(): ForegroundWindow? = window
}
