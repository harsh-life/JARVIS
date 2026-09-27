package com.hypermind.jarvis.perception

import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.contract.ActionResult
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ScreenNode
import com.hypermind.jarvis.contract.resultObject
import kotlinx.serialization.json.jsonPrimitive

/** The platform side of the actions that are not node actions. */
interface DeviceActions {
    fun globalAction(name: String): Boolean

    fun launch(packageName: String): Boolean
}

/**
 * The UI execution primitives (docs/23 §5.3, 08 §7): `accessibility.tap`,
 * `accessibility.swipe`, `accessibility.input_text`,
 * `accessibility.global_action` and `android.intent.launch_activity`.
 *
 * Each runs only after the guard allowed it (right device, fresh, in the
 * mapping, the app classified and its UI toggle on) and acts **only inside the
 * app the operation names**: that app must be the one in front, or the
 * operation is refused `package_mismatch` — never carried out in whatever app
 * happens to be showing. Targets are found by selector in the live
 * Accessibility tree; an absent target fails as an observation
 * (`target_not_found`), never a guess and never a coordinate tap.
 *
 * Text is never typed into a password field: credentials do not travel
 * through the agent (that is a login the user performs, 03 §3).
 */
class UiActions(
    private val screen: ScreenSource,
    private val device: DeviceActions,
    private val extractor: TreeExtractor = TreeExtractor(),
) {
    val tap = Primitive { envelope, _ -> tap(envelope) }
    val swipe = Primitive { envelope, _ -> swipe(envelope) }
    val inputText = Primitive { envelope, _ -> inputText(envelope) }
    val globalAction = Primitive { envelope, _ -> globalAction(envelope) }
    val launch = Primitive { envelope, _ -> launch(envelope) }

    private sealed interface Front {
        data class Ok(
            val tree: TreeExtractor.Extracted,
        ) : Front

        data class Stop(
            val result: ResultEnvelope,
        ) : Front
    }

    /** The named app in front, and its tree — or why not. */
    private fun front(envelope: OperationEnvelope): Front {
        val window =
            screen.foreground() ?: return Front.Stop(ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED))
        if (window.packageName != envelope.packageName) {
            return Front.Stop(ResultEnvelope.refused(envelope.opId, RefusalReason.PACKAGE_MISMATCH))
        }
        val root =
            window.root ?: return Front.Stop(ResultEnvelope.failed(envelope.opId, FailureReason.TARGET_NOT_FOUND))
        return Front.Ok(extractor.extract(root))
    }

    private fun done(
        envelope: OperationEnvelope,
        performed: Boolean,
        target: ScreenNode?,
    ): ResultEnvelope =
        if (performed) {
            ResultEnvelope.ok(envelope.opId, resultObject(ActionResult(target = target)))
        } else {
            ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        }

    private fun notFound(envelope: OperationEnvelope) =
        ResultEnvelope.failed(
            envelope.opId,
            FailureReason.TARGET_NOT_FOUND,
        )

    fun tap(envelope: OperationEnvelope): ResultEnvelope {
        val tree =
            when (val front = front(envelope)) {
                is Front.Stop -> return front.result
                is Front.Ok -> front.tree
            }
        val matched = Selector.from(envelope.arguments).find(tree.nodes) ?: return notFound(envelope)
        // A label is often inside the clickable row that owns it: act on the
        // nearest clickable ancestor, never on anything outside that chain.
        val target = clickableSelfOrAncestor(tree.nodes, matched) ?: return notFound(envelope)
        if (!target.enabled) return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        return done(envelope, tree.refs[target.id].perform(NodeAction.Click), target)
    }

    fun swipe(envelope: OperationEnvelope): ResultEnvelope {
        val tree =
            when (val front = front(envelope)) {
                is Front.Stop -> return front.result
                is Front.Ok -> front.tree
            }
        val direction =
            envelope.arguments
                .getValue("direction")
                .jsonPrimitive.content
        val viewId = envelope.arguments["view_id"]?.jsonPrimitive?.content
        val target =
            if (viewId != null) {
                Selector(viewId = viewId).find(tree.nodes)
            } else {
                tree.nodes.firstOrNull { it.scrollable }
            } ?: return notFound(envelope)
        return done(envelope, tree.refs[target.id].perform(NodeAction.Scroll(direction)), target)
    }

    fun inputText(envelope: OperationEnvelope): ResultEnvelope {
        val tree =
            when (val front = front(envelope)) {
                is Front.Stop -> return front.result
                is Front.Ok -> front.tree
            }
        val text =
            envelope.arguments
                .getValue("text")
                .jsonPrimitive.content
        val viewId = envelope.arguments["view_id"]?.jsonPrimitive?.content
        val target =
            if (viewId != null) {
                Selector(viewId = viewId).find(tree.nodes)
            } else {
                tree.nodes.indices.firstOrNull { tree.refs[it].focused && tree.nodes[it].editable }?.let(
                    tree.nodes::get,
                )
            } ?: return notFound(envelope)
        if (!target.editable || target.password || !target.enabled) {
            return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        }
        return done(envelope, tree.refs[target.id].perform(NodeAction.SetText(text)), target)
    }

    fun globalAction(envelope: OperationEnvelope): ResultEnvelope {
        when (val front = front(envelope)) {
            is Front.Stop -> return front.result
            is Front.Ok -> Unit
        }
        val action =
            envelope.arguments
                .getValue("action")
                .jsonPrimitive.content
        return done(envelope, device.globalAction(action), null)
    }

    fun launch(envelope: OperationEnvelope): ResultEnvelope {
        val pkg = envelope.packageName ?: return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        return done(envelope, device.launch(pkg), null)
    }

    companion object {
        fun clickableSelfOrAncestor(
            nodes: List<ScreenNode>,
            node: ScreenNode,
        ): ScreenNode? {
            var current: ScreenNode? = node
            while (current != null && !current.clickable) {
                current = current.parent?.let(nodes::getOrNull)
            }
            return current
        }
    }
}
