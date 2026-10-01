package com.hypermind.jarvis.tasks

import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.Reattestation
import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.auth.UserPresence
import com.hypermind.jarvis.contract.ConfirmationText
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.TaskView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException

/**
 * Tasks from this phone (02 §5; docs/23 §5.4). The device submits what the
 * user typed and shows what the server decided. For a pending action it only
 * relays the user's own answer through `/agent/tasks/{id}/confirm` with the
 * **server-issued** token — there is no local approval that could stand in
 * for it — and a `high_irreversible` approval is preceded by step-up (the
 * user's biometric or device credential, docs/23 §3).
 */
class TaskController(
    private val api: () -> ApiClient,
    private val accessToken: () -> String,
    private val stepUp: Reattestation,
) : TaskOperations {
    /** Why an approval did not go ahead. */
    sealed interface Outcome {
        data class Shown(
            val view: TaskView,
        ) : Outcome

        /** Step-up did not happen: nothing was sent, the action is still pending. */
        data class StepUpBlocked(
            val result: StepUpResult,
        ) : Outcome

        data object Unreachable : Outcome
    }

    override suspend fun submit(
        input: String,
        idempotencyKey: String,
        onSent: () -> Unit,
    ): Outcome = call { api().submitTask(accessToken(), input, idempotencyKey, onSent) }

    /** The server's current view of a task (owner-only). */
    override suspend fun refresh(taskId: String): Outcome = call { api().getTask(accessToken(), taskId) }

    /** Ask the server to cancel a live task. */
    override suspend fun cancel(taskId: String): Outcome = call { api().cancelTask(accessToken(), taskId) }

    /** Ask the server to run one of the owner's agents now (docs/29 §7.4); shown as the task it runs as. */
    override suspend fun runAgent(
        agentId: String,
        reminderDeliveryId: String?,
    ): Outcome = call { api().agents.runAgent(accessToken(), agentId, reminderDeliveryId) }

    override suspend fun decline(
        taskId: String,
        pending: PendingAction,
    ): Outcome {
        val token = tokenFor(taskId, pending) ?: return Outcome.Unreachable
        return call { api().confirmTask(accessToken(), taskId, token, approve = false) }
    }

    override suspend fun approve(
        taskId: String,
        pending: PendingAction,
        presence: UserPresence,
    ): Outcome {
        if (ConfirmationText.of(pending).needsStepUp) {
            val attested = stepUp.reattest(presence)
            if (attested !is StepUpResult.Attested) return Outcome.StepUpBlocked(attested)
        }
        val token = tokenFor(taskId, pending) ?: return Outcome.Unreachable
        return call { api().confirmTask(accessToken(), taskId, token, approve = true) }
    }

    /** A replayed response withholds the token; the owner fetches it from the task. */
    private suspend fun tokenFor(
        taskId: String,
        pending: PendingAction,
    ): String? {
        pending.confirmationToken?.let { return it }
        val fetched = (call { api().getTask(accessToken(), taskId) } as? Outcome.Shown)?.view ?: return null
        return (fetched as? TaskView.Result)?.result?.pending?.confirmationToken
    }

    private suspend fun call(block: () -> TaskView): Outcome =
        try {
            Outcome.Shown(withContext(Dispatchers.IO) { block() })
        } catch (ignored: IOException) {
            Outcome.Unreachable
        } catch (ignored: com.hypermind.jarvis.auth.EnrollmentLost) {
            // The device can no longer authenticate; the channel reports it as revoked.
            Outcome.Unreachable
        }
}

/** The server's task API as the tracker uses it (the tracker never decides anything itself). */
interface TaskOperations {
    suspend fun submit(
        input: String,
        idempotencyKey: String =
            java.util.UUID
                .randomUUID()
                .toString(),
        onSent: () -> Unit = {},
    ): TaskController.Outcome

    suspend fun refresh(taskId: String): TaskController.Outcome

    suspend fun cancel(taskId: String): TaskController.Outcome

    suspend fun approve(
        taskId: String,
        pending: PendingAction,
        presence: UserPresence,
    ): TaskController.Outcome

    suspend fun decline(
        taskId: String,
        pending: PendingAction,
    ): TaskController.Outcome

    /** docs/29 §7.4 / §17.1: the owner's own "Run" — on demand, or tapped from a reminder. */
    suspend fun runAgent(
        agentId: String,
        reminderDeliveryId: String?,
    ): TaskController.Outcome
}
