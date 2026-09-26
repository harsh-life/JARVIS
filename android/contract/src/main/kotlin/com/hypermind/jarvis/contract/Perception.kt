@file:UseSerializers(StrictIntSerializer::class, StrictBooleanSerializer::class, StrictDoubleSerializer::class)

package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * Typed device results — the mirror of the result models in
 * `shared/schemas/device_channel.py` (docs/23 §6). The client builds only
 * these, and each checks its own invariants on construction, so a result that
 * breaks a rule cannot even be built — above all, a password node that carries
 * text (ANDC-T6: redaction happens here, on the device, before anything is
 * serialized). The server re-validates every result regardless; the two are
 * held to the same `shared/android/perception_samples.json`.
 */
@Serializable
enum class ResultKind {
    @SerialName("screen_read")
    SCREEN_READ,

    @SerialName("action")
    ACTION,

    @SerialName("battery")
    BATTERY,

    @SerialName("notifications")
    NOTIFICATIONS,

    @SerialName("screenshot")
    SCREENSHOT,
}

private const val MAX_ROLE = 64
private const val MAX_VIEW_ID = 200
private const val MAX_PACKAGE = 255
private const val MAX_ACTIVITY = 255
private const val MAX_DIMENSION = 10_000
private const val BOUNDS_SIZE = 4

private fun requireText(
    value: String?,
    max: Int,
    field: String,
) = require(value == null || value.codePointCount(0, value.length) <= max) { "$field exceeds $max characters" }

@Serializable
data class ScreenNode(
    val id: Int,
    val parent: Int? = null,
    val role: String,
    val text: String? = null,
    @SerialName("content_description") val contentDescription: String? = null,
    @SerialName("view_id") val viewId: String? = null,
    val bounds: List<Int>,
    val clickable: Boolean = false,
    val editable: Boolean = false,
    val scrollable: Boolean = false,
    val enabled: Boolean = true,
    val checked: Boolean? = null,
    val selected: Boolean? = null,
    val password: Boolean = false,
) {
    init {
        require(id in 0 until Bounds.MAX_SCREEN_NODES) { "node id out of range" }
        require(parent == null || parent in 0 until Bounds.MAX_SCREEN_NODES) { "parent out of range" }
        requireText(role, MAX_ROLE, "role")
        requireText(text, Bounds.MAX_NODE_TEXT, "text")
        requireText(contentDescription, Bounds.MAX_NODE_TEXT, "content_description")
        requireText(viewId, MAX_VIEW_ID, "view_id")
        require(bounds.size == BOUNDS_SIZE) { "bounds are [left, top, right, bottom]" }
        require(!password || (text == null && contentDescription == null)) {
            "password nodes must be redacted on the device"
        }
    }
}

@Serializable
data class AppMetadata(
    @SerialName("package_name") val packageName: String,
    val activity: String? = null,
    @SerialName("window_title") val windowTitle: String? = null,
) {
    init {
        require(packageName.length <= MAX_PACKAGE && Bounds.PACKAGE_NAME.matches(packageName)) {
            "not an Android package name"
        }
        requireText(activity, MAX_ACTIVITY, "activity")
        requireText(windowTitle, Bounds.MAX_NODE_TEXT, "window_title")
    }
}

@Serializable
data class OcrBlock(
    val text: String,
    val bounds: List<Int>,
    val confidence: Double? = null,
) {
    init {
        requireText(text, Bounds.MAX_OCR_TEXT, "text")
        require(bounds.size == BOUNDS_SIZE) { "bounds are [left, top, right, bottom]" }
        require(confidence == null || confidence in 0.0..1.0) { "confidence is in [0, 1]" }
    }
}

@Serializable
data class ScreenReadResult(
    val app: AppMetadata,
    val nodes: List<ScreenNode> = emptyList(),
    @SerialName("ocr_blocks") val ocrBlocks: List<OcrBlock> = emptyList(),
    val truncated: Boolean = false,
) {
    init {
        require(nodes.size <= Bounds.MAX_SCREEN_NODES) { "too many nodes" }
        require(ocrBlocks.size <= Bounds.MAX_OCR_BLOCKS) { "too many OCR blocks" }
        val ids = nodes.map { it.id }
        require(ids.size == ids.toSet().size) { "node ids must be unique" }
        val known = ids.toSet()
        require(nodes.all { n -> n.parent == null || (n.parent in known && n.parent < n.id) }) {
            "a node's parent must be an earlier node"
        }
    }
}

@Serializable
data class BatteryState(
    @SerialName("level_percent") val levelPercent: Int,
    val charging: Boolean,
    val plugged: String,
) {
    init {
        require(levelPercent in 0..100) { "level is a percentage" }
        require(plugged in PLUGGED) { "unknown plug type" }
    }

    companion object {
        val PLUGGED = setOf("ac", "usb", "wireless", "dock", "none")
    }
}

@Serializable
data class NotificationItem(
    @SerialName("package_name") val packageName: String,
    val title: String? = null,
    val text: String? = null,
    @SerialName("posted_at") val postedAt: String,
) {
    init {
        require(packageName.length <= MAX_PACKAGE && Bounds.PACKAGE_NAME.matches(packageName)) {
            "not an Android package name"
        }
        requireText(title, Bounds.MAX_NODE_TEXT, "title")
        requireText(text, Bounds.MAX_NODE_TEXT, "text")
        require(
            try {
                OffsetDateTime.parse(postedAt)
                true
            } catch (ignored: DateTimeParseException) {
                false
            },
        ) { "posted_at is an ISO-8601 timestamp with an offset" }
    }
}

@Serializable
data class NotificationList(
    val notifications: List<NotificationItem> = emptyList(),
) {
    init {
        require(notifications.size <= Bounds.MAX_NOTIFICATIONS) { "too many notifications" }
    }
}

@Serializable
data class ActionResult(
    val performed: Boolean = true,
    val target: ScreenNode? = null,
) {
    init {
        require(performed) { "an action that did not happen is a failure, not an ok result" }
    }
}

@Serializable
data class ScreenshotResult(
    val app: AppMetadata,
    @SerialName("image_webp_base64") val imageWebpBase64: String,
    val width: Int,
    val height: Int,
) {
    init {
        require(imageWebpBase64.isNotEmpty() && imageWebpBase64.length <= Bounds.MAX_SCREENSHOT_FRAME_BYTES) {
            "image size out of range"
        }
        require(width in 1..MAX_DIMENSION && height in 1..MAX_DIMENSION) { "dimensions out of range" }
    }

    // The image must never reach a log line through a data-class toString.
    override fun toString(): String = "ScreenshotResult(app=${app.packageName}, ${width}x$height)"
}

/**
 * The rules a result must meet beyond its shape — the same ones the server's
 * `parse_observation` applies. The client runs this on its own result before
 * sending it; a result that fails is never sent (it becomes an explicit
 * failure instead).
 */
object PerceptionCheck {
    private val deviceRungs = setOf(PerceptionLevel.ACCESSIBILITY, PerceptionLevel.APP_METADATA, PerceptionLevel.OCR)

    /** Why [result] is not an acceptable `ok` result, or null. */
    fun problem(
        kind: ResultKind,
        result: JsonObject,
        level: PerceptionLevel?,
        packageName: String?,
    ): String? {
        val decoded =
            try {
                decode(kind, result)
            } catch (e: IllegalArgumentException) {
                // Includes SerializationException (a subclass): wrong types,
                // unknown fields, and every invariant the models check.
                return "does not match its schema: ${e.message}"
            }
        return levelProblem(decoded, level) ?: packageProblem(decoded, packageName)
    }

    private fun decode(
        kind: ResultKind,
        result: JsonObject,
    ): Any =
        when (kind) {
            ResultKind.SCREEN_READ -> ContractJson.decodeFromJsonElement(ScreenReadResult.serializer(), result)
            ResultKind.ACTION -> ContractJson.decodeFromJsonElement(ActionResult.serializer(), result)
            ResultKind.BATTERY -> ContractJson.decodeFromJsonElement(BatteryState.serializer(), result)
            ResultKind.NOTIFICATIONS -> ContractJson.decodeFromJsonElement(NotificationList.serializer(), result)
            ResultKind.SCREENSHOT -> ContractJson.decodeFromJsonElement(ScreenshotResult.serializer(), result)
        }

    private fun levelProblem(
        decoded: Any,
        level: PerceptionLevel?,
    ): String? {
        if (decoded !is ScreenReadResult) {
            return if (level != null) "only a screen read reports a perception level" else null
        }
        return when {
            level !in deviceRungs -> "a screen read reports the device rung it used"
            decoded.ocrBlocks.isNotEmpty() && level != PerceptionLevel.OCR -> "OCR text is reported at the ocr rung"
            level == PerceptionLevel.APP_METADATA && decoded.nodes.isNotEmpty() ->
                "the app_metadata rung carries no nodes"
            else -> null
        }
    }

    private fun packageProblem(
        decoded: Any,
        packageName: String?,
    ): String? {
        if (packageName == null) return null
        val app =
            when (decoded) {
                is ScreenReadResult -> decoded.app.packageName
                is ScreenshotResult -> decoded.app.packageName
                is NotificationList ->
                    return if (decoded.notifications.all { it.packageName == packageName }) {
                        null
                    } else {
                        "notifications from an app the operation did not name"
                    }
                else -> return null
            }
        return if (app == packageName) null else "result describes a different app than the operation named"
    }
}

/** Encode a typed result as the envelope's `result` object. */
inline fun <reified T> resultObject(value: T): JsonObject = ContractJson.encodeToJsonElement(value).jsonObject
