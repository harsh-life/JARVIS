package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.RiskCategory
import java.time.Instant

/**
 * What the phone is showing — docs/23 §7's single interface for the
 * presentation layer: `PresentationState {task_status, risk_tier_pending,
 * device_context, break_glass_active, error}`, derived from canonical server
 * task state and device state by [Presenter] and nothing else.
 *
 * It is **signals only**. There is deliberately no field for task content — no
 * user words, no model response, no pending-action arguments, no confirmation
 * token, no screen text — so whatever renders it (today a plain indicator and
 * the floating overlay; later the character) cannot display or leak content,
 * and can be replaced without touching the runtime, transport, auth,
 * execution, perception or task state. Content (a result, the canonical
 * confirmation card) is shown only by the task panel, from the task itself.
 *
 * It grants nothing: every field describes what the server decided or what the
 * device is doing. No UI action derives authority from it.
 */
data class PresentationState(
    val taskStatus: TaskPhase,
    /** The pending action's server-assigned risk tier, while one awaits the user. */
    val riskTierPending: RiskCategory? = null,
    /** The pending action needs a user-presence step-up before approval (docs/23 §3). */
    val stepUpRequired: Boolean = false,
    /** What a `waiting_for_platform` task is waiting for (the *task* waits; no operation does). */
    val waitingFor: WaitingFor? = null,
    val deviceContext: DeviceContext,
    val breakGlassActive: Boolean = false,
    val error: PresentationError? = null,
    /** A known, live task can be cancelled (server-side, `/cancel`). */
    val canCancel: Boolean = false,
    /** The last submission never got an answer and may be sent again unchanged (same idempotency key). */
    val canRetry: Boolean = false,
)

/** The headline state. Every value is derived; none is a local task state of its own. */
enum class TaskPhase {
    IDLE,
    LISTENING,
    SUBMITTING,
    THINKING,
    EXECUTING,
    WAITING_FOR_CONFIRMATION,
    WAITING_FOR_PLATFORM,
    COMPLETED,
    FAILED,
    BLOCKED,
    CANCELLED,
    OFFLINE,
    RECONNECTING,
}

/** What a waiting task needs back. `DEVICE_CHANNEL`: this phone was offline and was asked to reconnect. */
enum class WaitDependency {
    ACCESSIBILITY_SERVICE,
    SHIZUKU,
    NOTIFICATION_ACCESS,
    SCREEN_CAPTURE,
    OCR,
    DEVICE_CHANNEL,
    UNKNOWN,
    ;

    companion object {
        fun of(wire: String): WaitDependency =
            when (wire) {
                "accessibility_service" -> ACCESSIBILITY_SERVICE
                "shizuku" -> SHIZUKU
                "notification_access" -> NOTIFICATION_ACCESS
                "screen_capture" -> SCREEN_CAPTURE
                "ocr" -> OCR
                "device_channel" -> DEVICE_CHANNEL
                else -> UNKNOWN
            }
    }
}

data class WaitingFor(
    val dependency: WaitDependency,
    /** When the server stops waiting and fails the task; null if unparseable. */
    val until: Instant?,
    /** The dependency is available on this phone right now (the server resumes on its own). */
    val availableNow: Boolean?,
)

data class DeviceContext(
    val connection: ConnectionPhase,
    val platforms: Map<PlatformDependency, Boolean> = emptyMap(),
    val pushWake: PushWake = PushWake.NOT_OFFERED,
    /** What this phone is doing for a task right now — kind only, never content. */
    val activity: DeviceActivity? = null,
    val voice: VoiceActivity = VoiceActivity.NONE,
)

enum class ConnectionPhase {
    NOT_ENROLLED,
    CONNECTING,
    CONNECTED,
    RECONNECTING,
    AUTH_EXPIRED,
    OFFLINE,
    CHANNEL_DISABLED,
    UPDATE_REQUIRED,
    REVOKED,
}

enum class PushWake {
    NOT_OFFERED,
    OFF,
    ON,
    UNAVAILABLE,
}

/** docs/23 §6: which rung of the perception ladder, or which kind of action — for display only. */
enum class DeviceActivity {
    READING_SCREEN,
    READING_CONTROLS,
    USING_OCR,
    VISUAL_FALLBACK,
    READING_NOTIFICATIONS,
    READING_DEVICE_STATE,
    ACTING_IN_APP,
    PRIVILEGED_ACTION,
    WORKING,
}

/** docs/27: shown if a voice front end reports it; voice is never an authorization or confirmation signal. */
enum class VoiceActivity {
    NONE,
    LISTENING,
    TRANSCRIBING,
    SPEAKING,
}

data class PresentationError(
    val kind: ErrorKind,
    /** The server's machine code (e.g. `max_iterations_exceeded`), for the details view. Never a message. */
    val code: String? = null,
)

enum class ErrorKind {
    OFFLINE,
    SERVER_UNREACHABLE,
    SERVER_UNAVAILABLE,
    AUTH_EXPIRED,
    REVOKED,
    UPDATE_REQUIRED,
    CHANNEL_DISABLED,
    REFUSED,
    STOPPED_BY_OPERATOR,
    LIMIT_REACHED,
    CONFIRMATION_EXPIRED,
    STEP_UP_CANCELLED,
    STEP_UP_UNAVAILABLE,
    STEP_UP_FAILED,
    DEVICE_UNAVAILABLE,
    PLATFORM_UNAVAILABLE,
    OPERATION_EXPIRED,
    TASK_NOT_FOUND,
    INVALID_RESPONSE,
    TASK_FAILED,
}
