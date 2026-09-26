package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/**
 * The device channel wire contract — a field-for-field mirror of
 * `shared/schemas/device_channel.py` (docs/23 §4). Parsing is strict on both
 * sides: an unknown field is an error here exactly as `extra="forbid"` makes it
 * one on the server, so the two halves cannot drift apart silently.
 *
 * None of these types carries authority. An [OperationEnvelope] says what an
 * already-authorized server decision asks this device to do; the device may
 * only refuse it (docs/23 §0, §5.2).
 */
val ContractJson: Json =
    Json {
        ignoreUnknownKeys = false
        explicitNulls = false
        encodeDefaults = true
        isLenient = false
        coerceInputValues = false
    }

object Bounds {
    const val DEFAULT_OPERATION_TTL_SECONDS = 30L
    const val MAX_OPERATION_TTL_SECONDS = 60L
    const val MAX_RESULT_FRAME_BYTES = 64 * 1024
    const val MAX_SCREENSHOT_FRAME_BYTES = 2 * 1024 * 1024
    const val MAX_CONTROL_FRAME_BYTES = 8 * 1024
    const val MAX_SCREEN_NODES = 300
    const val MAX_NODE_TEXT = 500
    const val MAX_OCR_BLOCKS = 200
    const val MAX_OCR_TEXT = 1000
    const val MAX_NOTIFICATIONS = 50
    val PACKAGE_NAME = Regex("^[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+$")
}

@Serializable
enum class Mechanism {
    @SerialName("accessibility")
    ACCESSIBILITY,

    @SerialName("android_api")
    ANDROID_API,

    @SerialName("shizuku")
    SHIZUKU,
}

@Serializable
enum class PlatformDependency {
    @SerialName("accessibility_service")
    ACCESSIBILITY_SERVICE,

    @SerialName("shizuku")
    SHIZUKU,

    @SerialName("notification_access")
    NOTIFICATION_ACCESS,

    @SerialName("screen_capture")
    SCREEN_CAPTURE,

    @SerialName("ocr")
    OCR,
}

@Serializable
enum class GridToggle {
    @SerialName("screen_read")
    SCREEN_READ,

    @SerialName("ui_interaction")
    UI_INTERACTION,

    @SerialName("screenshot")
    SCREENSHOT,

    @SerialName("device_state")
    DEVICE_STATE,
}

@Serializable
enum class PerceptionLevel {
    @SerialName("accessibility")
    ACCESSIBILITY,

    @SerialName("app_metadata")
    APP_METADATA,

    @SerialName("ocr")
    OCR,

    @SerialName("vision")
    VISION,
}

@Serializable
enum class ResultStatus {
    @SerialName("ok")
    OK,

    @SerialName("refused")
    REFUSED,

    @SerialName("failed")
    FAILED,
}

@Serializable
enum class RefusalReason {
    @SerialName("wrong_device")
    WRONG_DEVICE,

    @SerialName("operation_expired")
    OPERATION_EXPIRED,

    @SerialName("mapping_version_mismatch")
    MAPPING_VERSION_MISMATCH,

    @SerialName("not_in_mapping")
    NOT_IN_MAPPING,

    @SerialName("toggle_off")
    TOGGLE_OFF,

    @SerialName("malformed_arguments")
    MALFORMED_ARGUMENTS,

    @SerialName("package_mismatch")
    PACKAGE_MISMATCH,

    @SerialName("sensitive_package")
    SENSITIVE_PACKAGE,

    @SerialName("secure_window")
    SECURE_WINDOW,

    @SerialName("platform_unavailable")
    PLATFORM_UNAVAILABLE,

    @SerialName("cancelled")
    CANCELLED,

    @SerialName("revoked")
    REVOKED,

    @SerialName("duplicate_operation")
    DUPLICATE_OPERATION,
}

@Serializable
enum class FailureReason {
    @SerialName("target_not_found")
    TARGET_NOT_FOUND,

    @SerialName("action_failed")
    ACTION_FAILED,

    @SerialName("timeout")
    TIMEOUT,

    @SerialName("internal")
    INTERNAL,
}

/** WebSocket close codes (RFC 6455 §7.4.2 application range). */
enum class CloseCode(
    val code: Int,
) {
    AUTH_FAILED(4001),
    AUTH_EXPIRED(4002),
    REVOKED(4003),
    MAPPING_VERSION_MISMATCH(4004),
    SUPERSEDED(4005),
    PROTOCOL_ERROR(4008),
    CHANNEL_DISABLED(4009),
    ;

    companion object {
        fun of(code: Int): CloseCode? = entries.firstOrNull { it.code == code }
    }
}

// ── server → device ─────────────────────────────────────────────────────

@Serializable
data class OperationEnvelope(
    val type: String = "operation",
    @SerialName("op_id") val opId: String,
    @SerialName("task_id") val taskId: String,
    @SerialName("device_id") val deviceId: String,
    val capability: String,
    val operation: String,
    val primitive: String,
    @SerialName("package_name") val packageName: String? = null,
    val arguments: JsonObject = JsonObject(emptyMap()),
    @SerialName("mapping_version") val mappingVersion: String,
    @SerialName("issued_at") val issuedAt: String,
    @SerialName("expires_at") val expiresAt: String,
)

@Serializable
data class CancelMessage(
    val type: String = "cancel",
    @SerialName("op_id") val opId: String? = null,
    @SerialName("task_id") val taskId: String? = null,
)

@Serializable
data class HelloAck(
    val type: String = "hello_ok",
    @SerialName("device_id") val deviceId: String,
    @SerialName("mapping_version") val mappingVersion: String,
    @SerialName("server_time") val serverTime: String,
    @SerialName("session_expires_at") val sessionExpiresAt: String,
)

@Serializable
data class WakePush(
    val type: String = "wake",
)

// ── device → server ─────────────────────────────────────────────────────

@Serializable
data class Hello(
    val type: String = "hello",
    @SerialName("access_token") val accessToken: String,
    @SerialName("device_proof") val deviceProof: String,
    @SerialName("mapping_version") val mappingVersion: String,
    @SerialName("client_version") val clientVersion: String,
) {
    // Credentials must never reach a log line through a data-class toString.
    override fun toString(): String = "Hello(mappingVersion=$mappingVersion, clientVersion=$clientVersion)"
}

@Serializable
data class Reauth(
    val type: String = "reauth",
    @SerialName("access_token") val accessToken: String,
) {
    override fun toString(): String = "Reauth(<redacted>)"
}

@Serializable
data class PlatformStatus(
    val type: String = "platform_status",
    val platforms: Map<PlatformDependency, Boolean>,
)

@Serializable
data class ResultEnvelope(
    val type: String = "result",
    @SerialName("op_id") val opId: String,
    val status: ResultStatus,
    @SerialName("refusal_reason") val refusalReason: RefusalReason? = null,
    @SerialName("failure_reason") val failureReason: FailureReason? = null,
    @SerialName("required_platform") val requiredPlatform: PlatformDependency? = null,
    val result: JsonObject? = null,
    @SerialName("perception_level") val perceptionLevel: PerceptionLevel? = null,
) {
    init {
        when (status) {
            ResultStatus.REFUSED -> {
                require(refusalReason != null && failureReason == null) { "refused carries a refusal_reason only" }
                require(result == null && perceptionLevel == null) { "refused carries no result" }
            }
            ResultStatus.FAILED -> {
                require(failureReason != null && refusalReason == null) { "failed carries a failure_reason only" }
                require(result == null) { "failed carries no result" }
            }
            ResultStatus.OK -> require(refusalReason == null && failureReason == null) { "ok carries no reason" }
        }
        require((refusalReason == RefusalReason.PLATFORM_UNAVAILABLE) == (requiredPlatform != null)) {
            "required_platform is set exactly for platform_unavailable"
        }
    }

    companion object {
        fun refused(
            opId: String,
            reason: RefusalReason,
            platform: PlatformDependency? = null,
        ) = ResultEnvelope(
            opId = opId,
            status = ResultStatus.REFUSED,
            refusalReason = reason,
            requiredPlatform = platform,
        )

        fun failed(
            opId: String,
            reason: FailureReason,
        ) = ResultEnvelope(opId = opId, status = ResultStatus.FAILED, failureReason = reason)

        fun ok(
            opId: String,
            result: JsonObject,
            level: PerceptionLevel? = null,
        ) = ResultEnvelope(opId = opId, status = ResultStatus.OK, result = result, perceptionLevel = level)
    }
}
