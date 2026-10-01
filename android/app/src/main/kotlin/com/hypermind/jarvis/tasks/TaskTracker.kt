package com.hypermind.jarvis.tasks

import android.content.SharedPreferences
import com.hypermind.jarvis.auth.UserPresence
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.presentation.TaskSnapshot
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.UUID

/**
 * The phone's current task, at app scope (docs/23 §7) — so a rotated screen,
 * a recreated activity or the overlay all see the same thing, and so nothing
 * is ever submitted twice.
 *
 * It is **not** a task runtime. Every state it holds is the server's last
 * answer ([TaskSnapshot]); it never decides, approves, resumes or re-runs
 * anything on its own:
 *
 * * One task at a time. A submit while another is in flight or live is
 *   refused here (the server would limit it too).
 * * A submission that got no answer can be sent again only unchanged — the
 *   same Idempotency-Key — so the server returns the original result instead
 *   of running it twice (02 §1.4).
 * * While the server says the task is running or waiting for a platform
 *   dependency, it asks the server again every [pollMillis] (the server
 *   resumes the task by itself; this only shows it).
 * * Approve / decline go through [TaskOperations] — the canonical
 *   confirmation with the server-issued token, step-up first when required.
 * * Only the current task's id is kept across process death ([TaskMemory]),
 *   so the phone can ask the server about it again — never its content.
 * * An agent run (docs/29 §7.4, §17.1) is the owner's "Run", shown as the
 *   ordinary task it runs as. A run tapped from a reminder that got no answer
 *   may be asked for again unchanged (the server makes the same tap one run);
 *   a run on demand never is — a lost answer may already have started one.
 */
class TaskTracker(
    private val tasks: TaskOperations,
    private val memory: TaskMemory,
    private val scope: CoroutineScope,
    private val pollMillis: Long = 3_000,
) {
    private val _snapshot = MutableStateFlow<TaskSnapshot>(TaskSnapshot.Idle)
    val snapshot: StateFlow<TaskSnapshot> = _snapshot.asStateFlow()

    private val _busy = MutableStateFlow(false)

    /** A request to the server is in flight; the UI disables actions meanwhile. */
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    private var unanswered: Pair<String, String>? = null // (input, idempotency key)
    private var unansweredTap: Pair<String, String>? = null // (agent id, reminder delivery id)
    private var poller: Job? = null

    @Synchronized
    fun canSubmit(): Boolean = !_busy.value && terminal(_snapshot.value)

    /** Submit [input]. False (nothing sent) when a task is already in flight or live. */
    @Synchronized
    fun submit(input: String): Boolean {
        if (input.isBlank() || !canSubmit()) return false
        val key = UUID.randomUUID().toString()
        unansweredTap = null
        unanswered = input to key
        send(input, key)
        return true
    }

    /** Run one of the owner's agents. False (nothing sent) when a task is already in flight or live. */
    @Synchronized
    fun runAgent(
        agentId: String,
        reminderDeliveryId: String?,
    ): Boolean {
        if (!canSubmit()) return false
        unanswered = null
        unansweredTap = reminderDeliveryId?.let { agentId to it }
        sendRun(agentId, reminderDeliveryId)
        return true
    }

    /** Send the unanswered submission (or reminder tap) again, unchanged: never run twice. */
    @Synchronized
    fun retry(): Boolean {
        if (_busy.value || _snapshot.value != TaskSnapshot.Unreachable) return false
        unansweredTap?.let { (agentId, deliveryId) ->
            sendRun(agentId, deliveryId)
            return true
        }
        val (input, key) = unanswered ?: return false
        send(input, key)
        return true
    }

    private fun sendRun(
        agentId: String,
        reminderDeliveryId: String?,
    ) {
        _busy.value = true
        _snapshot.value = TaskSnapshot.InFlight
        scope.launch {
            settle(tasks.runAgent(agentId, reminderDeliveryId), fromSubmit = true, previous = TaskSnapshot.Idle)
        }
    }

    private fun send(
        input: String,
        key: String,
    ) {
        _busy.value = true
        _snapshot.value = TaskSnapshot.Submitting
        scope.launch {
            val outcome =
                tasks.submit(input, key) {
                    synchronized(this@TaskTracker) {
                        if (_snapshot.value == TaskSnapshot.Submitting) _snapshot.value = TaskSnapshot.InFlight
                    }
                }
            settle(outcome, fromSubmit = true, previous = TaskSnapshot.Idle)
        }
    }

    fun approve(
        taskId: String,
        pending: PendingAction,
        presence: UserPresence,
    ): Boolean = act { tasks.approve(taskId, pending, presence) }

    fun decline(
        taskId: String,
        pending: PendingAction,
    ): Boolean = act { tasks.decline(taskId, pending) }

    /** Ask the server to cancel the current live task. */
    fun cancel(): Boolean {
        val id = synchronized(this) { liveTaskId(_snapshot.value) } ?: return false
        return act { tasks.cancel(id) }
    }

    /** Ask the server again about the current task (e.g. on returning to the app). */
    fun refresh(): Boolean {
        val id = synchronized(this) { taskId(_snapshot.value) ?: memory.taskId } ?: return false
        return act(showWorking = false) { tasks.refresh(id) }
    }

    /** Leave a finished task (or an unanswered submission) behind; back to idle. */
    @Synchronized
    fun dismiss(): Boolean {
        if (_busy.value || !(terminal(_snapshot.value) || _snapshot.value == TaskSnapshot.Unreachable)) return false
        unanswered = null
        unansweredTap = null
        memory.taskId = null
        _snapshot.value = TaskSnapshot.Idle
        return true
    }

    /** After a restart: pick the remembered task up again from the server. */
    @Synchronized
    fun reattach(): Boolean {
        if (_snapshot.value != TaskSnapshot.Idle || memory.taskId == null) return false
        return refresh()
    }

    /** Revocation: forget the task entirely. */
    @Synchronized
    fun wipe() {
        poller?.cancel()
        unanswered = null
        unansweredTap = null
        memory.taskId = null
        _busy.value = false
        _snapshot.value = TaskSnapshot.Idle
    }

    private fun act(
        showWorking: Boolean = true,
        call: suspend () -> TaskController.Outcome,
    ): Boolean {
        val previous: TaskSnapshot
        synchronized(this) {
            if (_busy.value) return false
            previous = _snapshot.value
            _busy.value = true
            if (showWorking) _snapshot.value = TaskSnapshot.InFlight
        }
        scope.launch { settle(call(), fromSubmit = false, previous = previous) }
        return true
    }

    @Synchronized
    private fun settle(
        outcome: TaskController.Outcome,
        fromSubmit: Boolean,
        previous: TaskSnapshot,
    ) {
        _busy.value = false
        when (outcome) {
            is TaskController.Outcome.Shown -> show(outcome.view)
            is TaskController.Outcome.StepUpBlocked ->
                (previous.pendingOf())?.let { (id, pending) ->
                    _snapshot.value = TaskSnapshot.StepUpBlocked(id, pending, outcome.result)
                } ?: run { _snapshot.value = previous }
            TaskController.Outcome.Unreachable ->
                when {
                    fromSubmit -> _snapshot.value = TaskSnapshot.Unreachable
                    previous is TaskSnapshot.Known -> {
                        // Unknown now, not changed: keep the last answer, marked stale, and keep asking.
                        val stale = previous.copy(unreachable = true)
                        _snapshot.value = stale
                        schedulePoll(stale)
                    }
                    else -> _snapshot.value = previous
                }
        }
    }

    private fun show(view: TaskView) {
        unanswered = null
        unansweredTap = null
        if (view is TaskView.Failed && view.code == "not_found") {
            memory.taskId = null
            _snapshot.value = TaskSnapshot.Idle
            return
        }
        val known = TaskSnapshot.Known(view)
        _snapshot.value = known
        memory.taskId = liveTaskId(known)
        schedulePoll(known)
    }

    private fun schedulePoll(known: TaskSnapshot.Known) {
        poller?.cancel()
        val result = (known.view as? TaskView.Result)?.result ?: return
        if (result.status != TaskStatus.RUNNING && result.status != TaskStatus.WAITING_FOR_PLATFORM) return
        poller =
            scope.launch {
                delay(pollMillis)
                if (!isActive) return@launch
                val still =
                    synchronized(this@TaskTracker) {
                        !_busy.value && taskId(_snapshot.value) == result.taskId
                    }
                if (still) refresh()
            }
    }

    companion object {
        /** No live task: a new one may be submitted. */
        fun terminal(snapshot: TaskSnapshot): Boolean =
            when (snapshot) {
                TaskSnapshot.Idle -> true
                is TaskSnapshot.Known ->
                    when (val view = snapshot.view) {
                        is TaskView.Failed -> true
                        is TaskView.NeedsConfirmation -> false
                        is TaskView.Result ->
                            view.result.status in setOf(TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
                    }
                else -> false
            }

        fun taskId(snapshot: TaskSnapshot): String? =
            when (snapshot) {
                is TaskSnapshot.Known ->
                    when (val view = snapshot.view) {
                        is TaskView.Result -> view.result.taskId
                        is TaskView.NeedsConfirmation -> view.taskId
                        is TaskView.Failed -> view.taskId
                    }
                is TaskSnapshot.StepUpBlocked -> snapshot.taskId
                else -> null
            }

        fun liveTaskId(snapshot: TaskSnapshot): String? = if (terminal(snapshot)) null else taskId(snapshot)

        private fun TaskSnapshot.pendingOf(): Pair<String, PendingAction>? =
            when (this) {
                is TaskSnapshot.StepUpBlocked -> taskId to pending
                is TaskSnapshot.Known ->
                    when (val v = view) {
                        is TaskView.NeedsConfirmation -> v.taskId to v.pending
                        is TaskView.Result -> v.result.pending?.let { v.result.taskId to it }
                        is TaskView.Failed -> null
                    }
                else -> null
            }
    }
}

/** The current task's id — and nothing else — across process death. */
class TaskMemory(
    private val prefs: SharedPreferences,
) {
    @get:Synchronized @set:Synchronized
    var taskId: String?
        get() = prefs.getString(KEY, null)?.takeIf { UUID_FORM.matches(it) }
        set(value) {
            prefs
                .edit()
                .apply { if (value == null || !UUID_FORM.matches(value)) remove(KEY) else putString(KEY, value) }
                .commit()
        }

    private companion object {
        const val KEY = "current_task_id"
        val UUID_FORM = Regex("^[0-9a-fA-F-]{36}$")
    }
}
