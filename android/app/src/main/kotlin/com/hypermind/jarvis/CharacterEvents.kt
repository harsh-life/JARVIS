package com.hypermind.jarvis

import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.presentation.TaskSnapshot
import kotlinx.coroutines.flow.Flow

/**
 * OD-CHAR-2: the one place a verified task event reaches the character. It
 * only *reads* the task tracker's snapshot — the server's own answer — and
 * never acts on a task; the character package itself cannot see tasks.
 */
object CharacterEvents {
    /**
     * A task has just completed: this snapshot is the server's `completed`
     * answer and the previous one seen was not that same answer. The first
     * snapshot seen never counts, so re-attaching to a task that finished
     * earlier (app restart, rotation) is not a fresh completion.
     */
    fun freshCompletion(
        previous: TaskSnapshot?,
        current: TaskSnapshot,
    ): Boolean {
        if (previous == null) return false
        val done = completedTask(current) ?: return false
        return completedTask(previous) != done
    }

    /** Calls [onCompleted] once per fresh completion in [snapshots]. */
    suspend fun forwardCompletions(
        snapshots: Flow<TaskSnapshot>,
        onCompleted: () -> Unit,
    ) {
        var previous: TaskSnapshot? = null
        snapshots.collect { current ->
            if (freshCompletion(previous, current)) onCompleted()
            previous = current
        }
    }

    private fun completedTask(snapshot: TaskSnapshot): String? =
        ((snapshot as? TaskSnapshot.Known)?.view as? TaskView.Result)
            ?.result
            ?.takeIf { it.status == TaskStatus.COMPLETED }
            ?.taskId
}
