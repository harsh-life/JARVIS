package com.hypermind.jarvis.perception

import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.contract.AppMetadata
import com.hypermind.jarvis.contract.Bounds
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OcrBlock
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ScreenNode
import com.hypermind.jarvis.contract.ScreenReadResult
import com.hypermind.jarvis.contract.resultObject

/**
 * `accessibility.read_tree` and `accessibility.read_element` — the perception
 * ladder of docs/23 §6, climbed only as far as needed:
 *
 * 1. **Accessibility tree** — bounded, password nodes redacted on the device.
 *    If it carries readable text, stop here.
 * 2. **App metadata** — package, activity, window title. Always present.
 * 3. **On-device OCR** — only when the tree says nothing readable. The frame
 *    is read and released on the device; only text is returned, and text
 *    inside a password field's bounds is dropped.
 *
 * A screenshot is never part of this: it is its own operation, with its own
 * toggle (docs/23 §6 level 4). Nothing read here is stored.
 *
 * Every read first checks that the app the operation names is the one in
 * front — never another app's screen instead (`package_mismatch`).
 */
class ScreenPerception(
    private val screen: ScreenSource,
    private val ocr: ScreenOcr?,
    private val extractor: TreeExtractor = TreeExtractor(),
) {
    val readTree = Primitive { envelope, spec -> readTree(envelope, spec) }
    val readElement = Primitive { envelope, spec -> readElement(envelope, spec) }

    private sealed interface Front {
        data class Ok(
            val window: ForegroundWindow,
        ) : Front

        data class Refuse(
            val result: ResultEnvelope,
        ) : Front
    }

    private fun front(envelope: OperationEnvelope): Front {
        val window =
            screen.foreground()
                ?: return Front.Refuse(ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED))
        if (window.packageName != envelope.packageName) {
            return Front.Refuse(ResultEnvelope.refused(envelope.opId, RefusalReason.PACKAGE_MISMATCH))
        }
        return Front.Ok(window)
    }

    private fun metadata(window: ForegroundWindow) =
        AppMetadata(
            packageName = window.packageName,
            activity = TreeExtractor.clip(window.activity, MAX_ACTIVITY),
            windowTitle = TreeExtractor.clip(window.windowTitle, Bounds.MAX_NODE_TEXT),
        )

    suspend fun readTree(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope {
        val window =
            when (val front = front(envelope)) {
                is Front.Refuse -> return front.result
                is Front.Ok -> front.window
            }
        val app = metadata(window)
        val tree = window.root?.let(extractor::extract) ?: TreeExtractor.Extracted(emptyList(), false, emptyList())
        if (tree.informative) {
            return fitted(
                envelope,
                spec,
                ScreenReadResult(app, tree.nodes, truncated = tree.truncated),
                PerceptionLevel.ACCESSIBILITY,
            )
        }
        val blocks = ocr?.readScreen()?.let { ocrBlocks(it, tree.passwordBounds) }.orEmpty()
        // The frame was taken after the tree: if another app came to the front
        // in between, its text is not this operation's to return.
        if (blocks.isNotEmpty() && screen.foreground()?.packageName != envelope.packageName) {
            return ResultEnvelope.refused(envelope.opId, RefusalReason.PACKAGE_MISMATCH)
        }
        if (blocks.isNotEmpty()) {
            return fitted(
                envelope,
                spec,
                ScreenReadResult(app, tree.nodes, blocks, truncated = tree.truncated),
                PerceptionLevel.OCR,
            )
        }
        return fitted(envelope, spec, ScreenReadResult(app), PerceptionLevel.APP_METADATA)
    }

    suspend fun readElement(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope {
        val window =
            when (val front = front(envelope)) {
                is Front.Refuse -> return front.result
                is Front.Ok -> front.window
            }
        val tree =
            window.root?.let(extractor::extract)
                ?: return ResultEnvelope.failed(envelope.opId, FailureReason.TARGET_NOT_FOUND)
        val target =
            Selector.from(envelope.arguments).find(tree.nodes)
                // 08 §7: an absent target is an observation, never a guess.
                ?: return ResultEnvelope.failed(envelope.opId, FailureReason.TARGET_NOT_FOUND)
        return fitted(
            envelope,
            spec,
            ScreenReadResult(metadata(window), subtree(tree.nodes, target)),
            PerceptionLevel.ACCESSIBILITY,
        )
    }

    companion object {
        private const val MAX_ACTIVITY = 255

        /** Headroom for the envelope around the result object. */
        private const val ENVELOPE_OVERHEAD = 512

        /** [target] and its descendants, renumbered from 0 with [target] as the root. */
        fun subtree(
            nodes: List<ScreenNode>,
            target: ScreenNode,
        ): List<ScreenNode> {
            val remap = mutableMapOf(target.id to 0)
            val out = mutableListOf(target.copy(id = 0, parent = null))
            // Pre-order: every descendant follows its parent in the list.
            for (node in nodes.filter { it.id > target.id }) {
                val parent = node.parent?.let(remap::get) ?: continue
                remap[node.id] = out.size
                out += node.copy(id = out.size, parent = parent)
            }
            return out
        }

        /** OCR text as contract blocks: clipped, bounded, none inside a password field. */
        fun ocrBlocks(
            texts: List<OcrText>,
            passwordBounds: List<List<Int>>,
        ): List<OcrBlock> =
            texts
                .asSequence()
                .filter { t -> passwordBounds.none { overlaps(it, t.bounds) } }
                .mapNotNull { t ->
                    TreeExtractor.clip(t.text, Bounds.MAX_OCR_TEXT)?.let {
                        OcrBlock(it, t.bounds, t.confidence?.coerceIn(0.0, 1.0))
                    }
                }.take(Bounds.MAX_OCR_BLOCKS)
                .toList()

        private fun overlaps(
            a: List<Int>,
            b: List<Int>,
        ): Boolean = a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]

        /**
         * The result as an `ok` envelope within the primitive's byte bound.
         * Over the bound, trailing OCR blocks and then trailing nodes are
         * dropped and `truncated` set — a suffix, so no node loses its parent.
         */
        fun fitted(
            envelope: OperationEnvelope,
            spec: PrimitiveSpec,
            result: ScreenReadResult,
            level: PerceptionLevel,
        ): ResultEnvelope {
            val budget = spec.maxResultBytes - ENVELOPE_OVERHEAD
            var current = result
            while (size(current) > budget) {
                current =
                    when {
                        current.ocrBlocks.isNotEmpty() ->
                            current.copy(
                                ocrBlocks = current.ocrBlocks.dropLast(dropCount(current.ocrBlocks.size)),
                                truncated = true,
                            )
                        current.nodes.isNotEmpty() ->
                            current.copy(
                                nodes = current.nodes.dropLast(dropCount(current.nodes.size)),
                                truncated = true,
                            )
                        else -> return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
                    }
            }
            // Dropping every OCR block leaves no text at the OCR rung: report the
            // rung the result now actually reflects.
            val actual =
                when {
                    level != PerceptionLevel.OCR || current.ocrBlocks.isNotEmpty() -> level
                    current.nodes.isNotEmpty() -> PerceptionLevel.ACCESSIBILITY
                    else -> PerceptionLevel.APP_METADATA
                }
            return ResultEnvelope.ok(envelope.opId, resultObject(current), actual)
        }

        private fun dropCount(size: Int) = maxOf(1, size / 8)

        private fun size(result: ScreenReadResult): Int =
            ContractJson.encodeToString(ScreenReadResult.serializer(), result).toByteArray(Charsets.UTF_8).size
    }
}
