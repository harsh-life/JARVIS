package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.channel.ReconnectCause
import com.hypermind.jarvis.contract.AgentResult
import com.hypermind.jarvis.contract.ConfirmationText
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * The one mapping: canonical server/device state → [PresentationState]
 * (docs/23 §7). Pure and deterministic — every screen, the overlay and any
 * future character read its output instead of interpreting server state
 * themselves, so no two surfaces can disagree about what is happening.
 *
 * Precedence, highest first:
 * 1. a revoked device is blocked, whatever else is known;
 * 2. the server's answer about the current task;
 * 3. the device's own connection state (idle / offline / reconnecting).
 *
 * Unknown server values never map to success: an unrecognised failure is a
 * failure, an unrecognised status is an error.
 */
object Presenter {
    fun of(inputs: PresentationInputs): PresentationState {
        val connection = connection(inputs.enrolled, inputs.channel)
        val context =
            DeviceContext(
                connection = connection,
                platforms = inputs.platforms,
                pushWake = pushWake(inputs.push),
                activity = activity(inputs.activeOperations, inputs.perceptionLevel),
                voice = inputs.voice,
            )
        if (connection == ConnectionPhase.REVOKED) {
            return PresentationState(
                TaskPhase.BLOCKED,
                deviceContext = context,
                error = PresentationError(ErrorKind.REVOKED),
            )
        }
        return when (val task = inputs.task) {
            TaskSnapshot.Idle -> idle(context)
            TaskSnapshot.Submitting -> PresentationState(TaskPhase.SUBMITTING, deviceContext = context)
            TaskSnapshot.InFlight -> PresentationState(working(context), deviceContext = context)
            TaskSnapshot.Unreachable ->
                PresentationState(
                    if (connection == ConnectionPhase.CONNECTED) TaskPhase.FAILED else TaskPhase.OFFLINE,
                    deviceContext = context,
                    error = PresentationError(ErrorKind.SERVER_UNREACHABLE),
                    canRetry = true,
                )
            is TaskSnapshot.StepUpBlocked ->
                confirmation(task.pending, context).copy(error = PresentationError(stepUpError(task.result)))
            is TaskSnapshot.Known -> known(task.view, context, inputs.platforms)
        }
    }

    private fun idle(context: DeviceContext): PresentationState {
        val (phase, error) =
            when (context.connection) {
                ConnectionPhase.CONNECTED -> TaskPhase.IDLE to null
                ConnectionPhase.CONNECTING, ConnectionPhase.RECONNECTING -> TaskPhase.RECONNECTING to null
                ConnectionPhase.AUTH_EXPIRED -> TaskPhase.RECONNECTING to PresentationError(ErrorKind.AUTH_EXPIRED)
                ConnectionPhase.OFFLINE, ConnectionPhase.NOT_ENROLLED -> TaskPhase.OFFLINE to null
                ConnectionPhase.CHANNEL_DISABLED -> TaskPhase.OFFLINE to PresentationError(ErrorKind.CHANNEL_DISABLED)
                ConnectionPhase.UPDATE_REQUIRED -> TaskPhase.BLOCKED to PresentationError(ErrorKind.UPDATE_REQUIRED)
                ConnectionPhase.REVOKED -> TaskPhase.BLOCKED to PresentationError(ErrorKind.REVOKED)
            }
        val listening = phase == TaskPhase.IDLE && context.voice != VoiceActivity.NONE
        return PresentationState(if (listening) TaskPhase.LISTENING else phase, deviceContext = context, error = error)
    }

    /** The server is running the task: executing if this phone is doing part of it right now. */
    private fun working(context: DeviceContext): TaskPhase =
        if (context.activity != null) TaskPhase.EXECUTING else TaskPhase.THINKING

    private fun confirmation(
        pending: PendingAction,
        context: DeviceContext,
    ) = PresentationState(
        TaskPhase.WAITING_FOR_CONFIRMATION,
        riskTierPending = pending.riskCategory,
        stepUpRequired = ConfirmationText.of(pending).needsStepUp,
        deviceContext = context,
        canCancel = true,
    )

    private fun known(
        view: TaskView,
        context: DeviceContext,
        platforms: Map<PlatformDependency, Boolean>,
    ): PresentationState =
        when (view) {
            is TaskView.NeedsConfirmation -> confirmation(view.pending, context)
            is TaskView.Failed -> failed(view.code, context)
            is TaskView.Result -> result(view.result, context, platforms)
        }

    private fun result(
        result: AgentResult,
        context: DeviceContext,
        platforms: Map<PlatformDependency, Boolean>,
    ): PresentationState {
        val base =
            PresentationState(TaskPhase.IDLE, deviceContext = context, breakGlassActive = result.breakGlassActive)
        return when (result.status) {
            TaskStatus.RUNNING -> base.copy(taskStatus = working(context), canCancel = true)
            TaskStatus.COMPLETED -> base.copy(taskStatus = TaskPhase.COMPLETED)
            TaskStatus.CANCELLED -> base.copy(taskStatus = TaskPhase.CANCELLED)
            TaskStatus.AWAITING_CONFIRMATION ->
                result.pending?.let { confirmation(it, context).copy(breakGlassActive = result.breakGlassActive) }
                    ?: base.copy(taskStatus = TaskPhase.FAILED, error = PresentationError(ErrorKind.INVALID_RESPONSE))
            TaskStatus.WAITING_FOR_PLATFORM ->
                base.copy(
                    taskStatus = TaskPhase.WAITING_FOR_PLATFORM,
                    waitingFor = result.waitingFor?.let { waitingFor(it.dependency, it.expiresAt, platforms, context) },
                    canCancel = true,
                )
            TaskStatus.FAILED -> {
                val code = result.failure?.code ?: "unknown"
                failed(code, context).copy(breakGlassActive = result.breakGlassActive)
            }
        }
    }

    private fun failed(
        code: String,
        context: DeviceContext,
    ): PresentationState {
        val kind = errorKind(code)
        val phase = if (kind in BLOCKING) TaskPhase.BLOCKED else TaskPhase.FAILED
        return PresentationState(phase, deviceContext = context, error = PresentationError(kind, code))
    }

    private fun waitingFor(
        wire: String,
        expiresAt: String,
        platforms: Map<PlatformDependency, Boolean>,
        context: DeviceContext,
    ): WaitingFor {
        val dependency = WaitDependency.of(wire)
        val available =
            when (dependency) {
                WaitDependency.DEVICE_CHANNEL -> context.connection == ConnectionPhase.CONNECTED
                WaitDependency.UNKNOWN -> null
                else -> platforms[platformOf(dependency)]
            }
        return WaitingFor(dependency, instant(expiresAt), available)
    }

    private fun platformOf(dependency: WaitDependency): PlatformDependency? =
        when (dependency) {
            WaitDependency.ACCESSIBILITY_SERVICE -> PlatformDependency.ACCESSIBILITY_SERVICE
            WaitDependency.SHIZUKU -> PlatformDependency.SHIZUKU
            WaitDependency.NOTIFICATION_ACCESS -> PlatformDependency.NOTIFICATION_ACCESS
            WaitDependency.SCREEN_CAPTURE -> PlatformDependency.SCREEN_CAPTURE
            WaitDependency.OCR -> PlatformDependency.OCR
            WaitDependency.DEVICE_CHANNEL, WaitDependency.UNKNOWN -> null
        }

    /** Server failure codes (AgentFailureCode) and error codes (02 §1.7), plus execution codes, by kind. */
    fun errorKind(code: String): ErrorKind =
        when (code) {
            "unauthenticated", "token_expired" -> ErrorKind.AUTH_EXPIRED
            "unauthorized", "prohibited", "refused" -> ErrorKind.REFUSED
            "principal_revoked" -> ErrorKind.REVOKED
            "emergency_stop" -> ErrorKind.STOPPED_BY_OPERATOR
            "rate_limited", "budget_exceeded" -> ErrorKind.LIMIT_REACHED
            "model_unavailable", "dependency_unavailable" -> ErrorKind.SERVER_UNAVAILABLE
            "confirmation_expired", "confirmation_state_lost" -> ErrorKind.CONFIRMATION_EXPIRED
            "platform_unavailable" -> ErrorKind.PLATFORM_UNAVAILABLE
            "device_unavailable" -> ErrorKind.DEVICE_UNAVAILABLE
            "operation_expired" -> ErrorKind.OPERATION_EXPIRED
            "not_found" -> ErrorKind.TASK_NOT_FOUND
            "malformed_response" -> ErrorKind.INVALID_RESPONSE
            else -> ErrorKind.TASK_FAILED
        }

    private val BLOCKING = setOf(ErrorKind.REFUSED, ErrorKind.REVOKED, ErrorKind.STOPPED_BY_OPERATOR)

    private fun stepUpError(result: StepUpResult): ErrorKind =
        when (result) {
            StepUpResult.Cancelled -> ErrorKind.STEP_UP_CANCELLED
            StepUpResult.NoKey -> ErrorKind.STEP_UP_UNAVAILABLE
            else -> ErrorKind.STEP_UP_FAILED
        }

    fun connection(
        enrolled: Boolean,
        channel: ChannelState,
    ): ConnectionPhase =
        when {
            channel is ChannelState.Revoked -> ConnectionPhase.REVOKED
            !enrolled -> ConnectionPhase.NOT_ENROLLED
            else ->
                when (channel) {
                    ChannelState.Stopped -> ConnectionPhase.OFFLINE
                    ChannelState.Connecting -> ConnectionPhase.CONNECTING
                    is ChannelState.Connected -> ConnectionPhase.CONNECTED
                    is ChannelState.Reconnecting ->
                        if (channel.cause == ReconnectCause.AUTHENTICATION_EXPIRED) {
                            ConnectionPhase.AUTH_EXPIRED
                        } else {
                            ConnectionPhase.RECONNECTING
                        }
                    is ChannelState.UpdateRequired -> ConnectionPhase.UPDATE_REQUIRED
                    ChannelState.Disabled -> ConnectionPhase.CHANNEL_DISABLED
                    ChannelState.Revoked -> ConnectionPhase.REVOKED
                }
        }

    private fun pushWake(push: PushStatus): PushWake =
        when (push) {
            PushStatus.NOT_OFFERED -> PushWake.NOT_OFFERED
            PushStatus.OFF -> PushWake.OFF
            PushStatus.ON -> PushWake.ON
            PushStatus.UNAVAILABLE -> PushWake.UNAVAILABLE
        }

    /** docs/23 §6: the kind of work, from the primitive (and the rung a read is on) — never its content. */
    fun activity(
        operations: List<ActiveOperation>,
        level: PerceptionLevel?,
    ): DeviceActivity? {
        val kinds = operations.map { kind(it.primitive, level) }
        return ACTIVITY_PRIORITY.firstOrNull { it in kinds }
    }

    private fun kind(
        primitive: String,
        level: PerceptionLevel?,
    ): DeviceActivity =
        when {
            primitive == "accessibility.read_tree" ->
                when (level) {
                    PerceptionLevel.OCR -> DeviceActivity.USING_OCR
                    PerceptionLevel.VISION -> DeviceActivity.VISUAL_FALLBACK
                    else -> DeviceActivity.READING_SCREEN
                }
            primitive == "accessibility.read_element" -> DeviceActivity.READING_CONTROLS
            primitive == "accessibility.screenshot" -> DeviceActivity.VISUAL_FALLBACK
            primitive == "android.api.notification_query" -> DeviceActivity.READING_NOTIFICATIONS
            primitive == "android.api.battery_state" -> DeviceActivity.READING_DEVICE_STATE
            primitive.startsWith("shizuku.") -> DeviceActivity.PRIVILEGED_ACTION
            primitive.startsWith("accessibility.") || primitive.startsWith("android.intent.") ->
                DeviceActivity.ACTING_IN_APP
            else -> DeviceActivity.WORKING
        }

    private val ACTIVITY_PRIORITY =
        listOf(
            DeviceActivity.PRIVILEGED_ACTION,
            DeviceActivity.ACTING_IN_APP,
            DeviceActivity.VISUAL_FALLBACK,
            DeviceActivity.USING_OCR,
            DeviceActivity.READING_SCREEN,
            DeviceActivity.READING_CONTROLS,
            DeviceActivity.READING_NOTIFICATIONS,
            DeviceActivity.READING_DEVICE_STATE,
            DeviceActivity.WORKING,
        )

    private fun instant(value: String): Instant? =
        try {
            OffsetDateTime.parse(value).toInstant()
        } catch (ignored: DateTimeParseException) {
            null
        }
}
