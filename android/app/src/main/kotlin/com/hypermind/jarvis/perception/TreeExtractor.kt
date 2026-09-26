package com.hypermind.jarvis.perception

import com.hypermind.jarvis.contract.Bounds
import com.hypermind.jarvis.contract.ScreenNode

/**
 * The Accessibility tree as a bounded, redacted node list (docs/23 §6 level 1).
 *
 * * **Bounded.** At most [maxNodes] nodes and [maxDepth] levels; anything cut
 *   off sets `truncated`. Every string is clipped to the contract's limits.
 * * **Redacted here, on the device.** A password node keeps its role, bounds
 *   and flags — enough to find and focus it — but its text and description
 *   are dropped before a [ScreenNode] exists (ANDC-T6). The model cannot
 *   even be built with them.
 * * **Pre-order ids.** A node's id is its position, so every parent precedes
 *   its children; dropping a suffix (to fit a byte budget) never orphans a
 *   node.
 * * Invisible subtrees are skipped: what is not on screen is not read.
 */
class TreeExtractor(
    private val maxNodes: Int = Bounds.MAX_SCREEN_NODES,
    private val maxDepth: Int = MAX_DEPTH,
) {
    data class Extracted(
        val nodes: List<ScreenNode>,
        val truncated: Boolean,
        /** Bounds of every password field seen — OCR text inside them is dropped. */
        val passwordBounds: List<List<Int>>,
    ) {
        /** Whether the tree says anything a reader could use. */
        val informative: Boolean get() =
            nodes.any {
                !it.text.isNullOrBlank() || !it.contentDescription.isNullOrBlank()
            }
    }

    private data class Pending(
        val node: A11yNode,
        val parent: Int?,
        val depth: Int,
    )

    fun extract(root: A11yNode): Extracted {
        val nodes = mutableListOf<ScreenNode>()
        val passwords = mutableListOf<List<Int>>()
        var truncated = false
        val stack = ArrayDeque<Pending>()
        stack.addLast(Pending(root, null, 0))
        while (stack.isNotEmpty()) {
            val (node, parent, depth) = stack.removeLast()
            val withinBounds = nodes.size < maxNodes && depth <= maxDepth
            if (node.visible && !withinBounds) truncated = true
            if (node.visible && withinBounds) {
                val id = nodes.size
                nodes += screenNode(node, id, parent)
                if (node.password) passwords += node.bounds
                pushChildren(stack, node, id, depth)
            }
        }
        return Extracted(nodes, truncated, passwords)
    }

    /** Reversed, so the first child is taken next (pre-order). */
    private fun pushChildren(
        stack: ArrayDeque<Pending>,
        node: A11yNode,
        id: Int,
        depth: Int,
    ) {
        for (i in node.childCount - 1 downTo 0) {
            node.child(i)?.let { stack.addLast(Pending(it, id, depth + 1)) }
        }
    }

    private fun screenNode(
        node: A11yNode,
        id: Int,
        parent: Int?,
    ): ScreenNode =
        ScreenNode(
            id = id,
            parent = parent,
            role = clip(node.className ?: "view", MAX_ROLE) ?: "view",
            // Redaction: a password field's content never leaves the device.
            text = if (node.password) null else clip(node.text, Bounds.MAX_NODE_TEXT),
            contentDescription = if (node.password) null else clip(node.contentDescription, Bounds.MAX_NODE_TEXT),
            viewId = clip(node.viewId, MAX_VIEW_ID),
            bounds = node.bounds,
            clickable = node.clickable,
            editable = node.editable,
            scrollable = node.scrollable,
            enabled = node.enabled,
            checked = if (node.checkable) node.checked else null,
            selected = if (node.selected) true else null,
            password = node.password,
        )

    companion object {
        const val MAX_DEPTH = 64
        private const val MAX_ROLE = 64
        private const val MAX_VIEW_ID = 200

        /** Clip to [max] code points without splitting a surrogate pair; blank → null. */
        fun clip(
            value: String?,
            max: Int,
        ): String? {
            val trimmed = value?.trim()
            if (trimmed.isNullOrEmpty()) return null
            if (trimmed.codePointCount(0, trimmed.length) <= max) return trimmed
            return trimmed.substring(0, trimmed.offsetByCodePoints(0, max))
        }
    }
}
