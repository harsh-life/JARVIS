package com.hypermind.jarvis.perception

import com.hypermind.jarvis.contract.ScreenNode
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive

/**
 * A UI target named the way the shared mapping allows — by view id, visible
 * text or content description, plus an optional index among matches. Never
 * by coordinates (08 §7). A password node never matches by text: it has
 * none on this side of redaction either.
 */
data class Selector(
    val viewId: String? = null,
    val text: String? = null,
    val contentDescription: String? = null,
    val index: Int = 0,
) {
    fun matches(node: ScreenNode): Boolean {
        val nodeViewId = node.viewId
        return when {
            viewId != null -> nodeViewId != null && (nodeViewId == viewId || nodeViewId.endsWith(":id/$viewId"))
            text != null -> node.text?.trim().equals(text.trim(), ignoreCase = true)
            contentDescription != null ->
                node.contentDescription?.trim().equals(
                    contentDescription.trim(),
                    ignoreCase = true,
                )
            else -> false
        }
    }

    /** The [index]-th matching node, or null (the target is absent). */
    fun find(nodes: List<ScreenNode>): ScreenNode? = nodes.filter(::matches).getOrNull(index)

    companion object {
        /** From arguments the guard has already validated against the mapping. */
        fun from(arguments: JsonObject): Selector =
            Selector(
                viewId = arguments["view_id"]?.jsonPrimitive?.content,
                text = arguments["text"]?.jsonPrimitive?.content,
                contentDescription = arguments["content_description"]?.jsonPrimitive?.content,
                index = arguments["index"]?.jsonPrimitive?.int ?: 0,
            )
    }
}
