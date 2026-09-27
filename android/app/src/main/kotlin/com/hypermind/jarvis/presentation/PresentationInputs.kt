package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.TaskView

/**
 * The phone's knowledge of its current task — held by the task tracker, fed
 * only by the server's answers. It is not a task runtime: nothing here
 * decides, retries or approves anything on its own.
 */
sealed interface TaskSnapshot {
    data object Idle : TaskSnapshot

    /** The submission is on its way to the server. */
    data object Submitting : TaskSnapshot

    /** The server has the task and is working on it (the answer has not come back yet). */
    data object InFlight : TaskSnapshot

    /**
     * The server's latest answer about this task. [unreachable]: the last
     * attempt to reach the server about it (a refresh, an approval, a cancel)
     * got no answer, so this may be out of date.
     */
    data class Known(
        val view: TaskView,
        val unreachable: Boolean = false,
    ) : TaskSnapshot

    /** No answer: the server could not be reached. The same submission may be sent again. */
    data object Unreachable : TaskSnapshot

    /** The user's approval did not go through step-up: nothing was sent; the action is still pending. */
    data class StepUpBlocked(
        val taskId: String,
        val pending: PendingAction,
        val result: StepUpResult,
    ) : TaskSnapshot
}

/** One operation this phone is running right now — its kind, never its arguments. */
data class ActiveOperation(
    val taskId: String,
    val capability: String,
    val operation: String,
    val primitive: String,
)

enum class PushStatus {
    NOT_OFFERED,
    OFF,
    ON,
    UNAVAILABLE,
}

/** Everything [Presenter] reads. All of it is canonical server or device state. */
data class PresentationInputs(
    val enrolled: Boolean,
    val channel: ChannelState,
    val task: TaskSnapshot = TaskSnapshot.Idle,
    val platforms: Map<PlatformDependency, Boolean> = emptyMap(),
    val activeOperations: List<ActiveOperation> = emptyList(),
    /** The perception rung the running read is on, if one is. */
    val perceptionLevel: PerceptionLevel? = null,
    val push: PushStatus = PushStatus.NOT_OFFERED,
    val voice: VoiceActivity = VoiceActivity.NONE,
)
