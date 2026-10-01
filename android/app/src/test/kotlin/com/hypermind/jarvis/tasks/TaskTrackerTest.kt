package com.hypermind.jarvis.tasks

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.auth.UserPresence
import com.hypermind.jarvis.contract.AgentResult
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.PlatformWait
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.presentation.TaskSnapshot
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File

/** The app-scope task state: the server's answers, one task at a time, never re-run (docs/23 §7). */
@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
class TaskTrackerTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val memory = TaskMemory(context.getSharedPreferences("task-${System.nanoTime()}", Context.MODE_PRIVATE))
    private val samples =
        ContractJson
            .parseToJsonElement(
                File(requireNotNull(System.getProperty("jarvis.shared.dir"))).resolve("task_samples.json").readText(),
            ).jsonObject
            .getValue("samples")
            .jsonArray
            .associate {
                it.jsonObject
                    .getValue("name")
                    .jsonPrimitive.content to it.jsonObject
            }

    private fun sample(name: String): TaskView {
        val s = samples.getValue(name)
        return TaskView.parse(s.getValue("http_status").jsonPrimitive.int, s.getValue("body").toString())
    }

    private val id = "6f1c2d3e-4a5b-4c6d-8e7f-00000000a001"

    private companion object {
        const val AGENT = "6f1c2d3e-4a5b-4c6d-8e7f-00000000a777"
    }

    /** The server, scripted: each call takes the next answer; records what was asked. */
    private class FakeServer : TaskOperations {
        val answers = ArrayDeque<CompletableDeferred<TaskController.Outcome>>()
        val submissions = mutableListOf<Pair<String, String>>()
        val calls = mutableListOf<String>()

        fun answer(outcome: TaskController.Outcome) = answers.addLast(CompletableDeferred(outcome))

        private suspend fun next(): TaskController.Outcome = answers.removeFirst().await()

        override suspend fun submit(
            input: String,
            idempotencyKey: String,
            onSent: () -> Unit,
        ): TaskController.Outcome {
            submissions += input to idempotencyKey
            calls += "submit"
            onSent()
            return next()
        }

        override suspend fun refresh(taskId: String) = next().also { calls += "refresh:$taskId" }

        override suspend fun cancel(taskId: String) = next().also { calls += "cancel:$taskId" }

        override suspend fun approve(
            taskId: String,
            pending: PendingAction,
            presence: UserPresence,
        ) = next().also { calls += "approve:$taskId" }

        override suspend fun decline(
            taskId: String,
            pending: PendingAction,
        ) = next().also { calls += "decline:$taskId" }

        override suspend fun runAgent(
            agentId: String,
            reminderDeliveryId: String?,
        ) = next().also { calls += "run:$agentId:$reminderDeliveryId" }
    }

    private val server = FakeServer()

    // On the test scheduler (not backgroundScope, whose work advanceUntilIdle skips).
    private fun TestScope.tracker() =
        TaskTracker(server, memory, CoroutineScope(StandardTestDispatcher(testScheduler) + Job()), pollMillis = 3_000)

    private fun shown(view: TaskView) = TaskController.Outcome.Shown(view)

    private fun result(
        status: TaskStatus,
        wait: PlatformWait? = null,
    ) = TaskView.Result(AgentResult(id, status, waitingFor = wait))

    @Test
    fun `a submission goes out once and the answer is shown`() =
        runTest {
            val tracker = tracker()
            val gate = CompletableDeferred<TaskController.Outcome>()
            server.answers.addLast(gate)
            assertTrue(tracker.submit("add milk"))
            runCurrent()
            assertEquals(TaskSnapshot.InFlight, tracker.snapshot.value) // on the wire: the server is working
            // No duplicate while it is in flight.
            assertFalse(tracker.submit("add milk"))
            assertFalse(tracker.canSubmit())
            gate.complete(shown(sample("completed")))
            runCurrent()
            assertTrue(tracker.snapshot.value is TaskSnapshot.Known)
            assertEquals(1, server.submissions.size)
            assertNull(memory.taskId) // finished: nothing to re-attach
        }

    @Test
    fun `a live task blocks a new submission until it ends`() =
        runTest {
            val tracker = tracker()
            server.answer(shown(sample("awaiting_confirmation")))
            tracker.submit("type a note")
            runCurrent()
            assertFalse(tracker.submit("something else"))
            assertEquals(id, memory.taskId) // remembered (id only) to re-attach after a restart
        }

    @Test
    fun `no answer allows only the same submission again, with the same key`() =
        runTest {
            val tracker = tracker()
            server.answer(TaskController.Outcome.Unreachable)
            tracker.submit("add milk")
            runCurrent()
            assertEquals(TaskSnapshot.Unreachable, tracker.snapshot.value)
            assertFalse(tracker.submit("something new")) // not while unanswered
            server.answer(shown(sample("completed")))
            assertTrue(tracker.retry())
            runCurrent()
            assertEquals(2, server.submissions.size)
            assertEquals(server.submissions[0], server.submissions[1]) // same input, same idempotency key
            assertFalse(tracker.retry()) // answered: nothing to retry
        }

    @Test
    fun `an agent run goes out once and the task it ran as is shown`() =
        runTest {
            val tracker = tracker()
            val gate = CompletableDeferred<TaskController.Outcome>()
            server.answers.addLast(gate)
            assertTrue(tracker.runAgent(AGENT, reminderDeliveryId = "d-1"))
            runCurrent()
            // Nothing else goes out while it is in flight: not the run again, not a task.
            assertFalse(tracker.runAgent(AGENT, reminderDeliveryId = "d-1"))
            assertFalse(tracker.submit("something"))
            gate.complete(shown(sample("awaiting_confirmation")))
            runCurrent()
            assertEquals(listOf("run:$AGENT:d-1"), server.calls)
            assertEquals(id, memory.taskId) // the run is an ordinary task: re-attached like one
            assertFalse(tracker.runAgent(AGENT, null)) // live: one at a time
        }

    @Test
    fun `a tapped run with no answer is asked for again unchanged, an on-demand one never`() =
        runTest {
            val tracker = tracker()
            server.answer(TaskController.Outcome.Unreachable)
            tracker.runAgent(AGENT, reminderDeliveryId = "d-1")
            runCurrent()
            assertEquals(TaskSnapshot.Unreachable, tracker.snapshot.value)
            server.answer(shown(sample("completed")))
            assertTrue(tracker.retry()) // the server makes the same tap the same run
            runCurrent()
            assertEquals(listOf("run:$AGENT:d-1", "run:$AGENT:d-1"), server.calls)
            assertTrue(tracker.dismiss())
            // On demand: a lost answer may already have started a run — never sent twice.
            server.answer(TaskController.Outcome.Unreachable)
            tracker.runAgent(AGENT, reminderDeliveryId = null)
            runCurrent()
            assertFalse(tracker.retry())
            assertEquals(3, server.calls.size)
        }

    @Test
    fun `dismissing an unanswered submission drops it`() =
        runTest {
            val tracker = tracker()
            server.answer(TaskController.Outcome.Unreachable)
            tracker.submit("add milk")
            runCurrent()
            assertTrue(tracker.dismiss())
            assertEquals(TaskSnapshot.Idle, tracker.snapshot.value)
            assertFalse(tracker.retry())
        }

    @Test
    fun `a waiting task is asked about again until the server moves it on`() =
        runTest {
            val tracker = tracker()
            val waiting = result(TaskStatus.WAITING_FOR_PLATFORM, PlatformWait("shizuku", "d", "2099-01-01T00:00:00Z"))
            server.answer(shown(waiting))
            tracker.submit("stop the app")
            runCurrent()
            server.answer(shown(waiting))
            advanceTimeBy(3_001)
            runCurrent()
            assertEquals(listOf("submit", "refresh:$id"), server.calls)
            server.answer(shown(result(TaskStatus.COMPLETED)))
            advanceTimeBy(3_001)
            runCurrent()
            assertEquals(
                TaskStatus.COMPLETED,
                ((tracker.snapshot.value as TaskSnapshot.Known).view as TaskView.Result).result.status,
            )
            advanceTimeBy(30_000)
            runCurrent()
            assertEquals(3, server.calls.size) // finished: no more polling
        }

    @Test
    fun `an unanswered refresh keeps the last answer, marked stale, and keeps asking`() =
        runTest {
            val tracker = tracker()
            server.answer(shown(result(TaskStatus.RUNNING)))
            tracker.submit("x")
            runCurrent()
            server.answer(TaskController.Outcome.Unreachable)
            advanceTimeBy(3_001)
            runCurrent()
            val stale = tracker.snapshot.value as TaskSnapshot.Known
            assertTrue(stale.unreachable)
            assertEquals(TaskStatus.RUNNING, (stale.view as TaskView.Result).result.status)
            server.answer(shown(result(TaskStatus.COMPLETED)))
            advanceTimeBy(3_001)
            runCurrent()
            assertFalse((tracker.snapshot.value as TaskSnapshot.Known).unreachable)
        }

    @Test
    fun `cancel asks the server and shows its answer`() =
        runTest {
            val tracker = tracker()
            server.answer(shown(sample("waiting_for_platform")))
            tracker.submit("x")
            runCurrent()
            server.answer(shown(result(TaskStatus.CANCELLED)))
            assertTrue(tracker.cancel())
            runCurrent()
            assertTrue(server.calls.contains("cancel:$id"))
            assertTrue(tracker.canSubmit())
            assertNull(memory.taskId)
        }

    @Test
    fun `nothing to cancel before the server named the task`() =
        runTest {
            val tracker = tracker()
            assertFalse(tracker.cancel())
            server.answers.addLast(CompletableDeferred()) // never answers
            tracker.submit("x")
            runCurrent()
            assertFalse(tracker.cancel())
        }

    @Test
    fun `a blocked step-up keeps the action pending, nothing sent`() =
        runTest {
            val tracker = tracker()
            server.answer(shown(sample("awaiting_step_up")))
            tracker.submit("x")
            runCurrent()
            val paused = (tracker.snapshot.value as TaskSnapshot.Known).view as TaskView.NeedsConfirmation
            server.answer(TaskController.Outcome.StepUpBlocked(StepUpResult.Cancelled))
            tracker.approve(paused.taskId, paused.pending) { it }
            runCurrent()
            val blocked = tracker.snapshot.value as TaskSnapshot.StepUpBlocked
            assertEquals(paused.pending, blocked.pending)
            assertEquals(StepUpResult.Cancelled, blocked.result)
        }

    @Test
    fun `approval and decline go to the server, one action at a time`() =
        runTest {
            val tracker = tracker()
            server.answer(shown(sample("awaiting_confirmation")))
            tracker.submit("x")
            runCurrent()
            val paused = (tracker.snapshot.value as TaskSnapshot.Known).view as TaskView.NeedsConfirmation
            val gate = CompletableDeferred<TaskController.Outcome>()
            server.answers.addLast(gate)
            assertTrue(tracker.approve(paused.taskId, paused.pending) { it })
            runCurrent()
            assertFalse(tracker.decline(paused.taskId, paused.pending)) // a second answer while one is in flight
            gate.complete(shown(sample("completed")))
            runCurrent()
            assertEquals(listOf("submit", "approve:$id"), server.calls)
        }

    @Test
    fun `after a restart the remembered task is picked up from the server`() =
        runTest {
            memory.taskId = id
            val tracker = tracker()
            server.answer(shown(sample("awaiting_confirmation")))
            assertTrue(tracker.reattach())
            runCurrent()
            assertEquals(listOf("refresh:$id"), server.calls.take(1))
            assertTrue(tracker.snapshot.value is TaskSnapshot.Known)
            // A task the server no longer knows is forgotten.
            memory.taskId = id
            val fresh = tracker()
            server.answers.clear()
            server.answer(shown(TaskView.Failed(id, "not_found", "gone")))
            fresh.reattach()
            runCurrent()
            assertEquals(TaskSnapshot.Idle, fresh.snapshot.value)
            assertNull(memory.taskId)
        }

    @Test
    fun `revocation forgets the task entirely`() =
        runTest {
            val tracker = tracker()
            server.answer(shown(sample("awaiting_confirmation")))
            tracker.submit("x")
            runCurrent()
            tracker.wipe()
            assertEquals(TaskSnapshot.Idle, tracker.snapshot.value)
            assertNull(memory.taskId)
        }

    @Test
    fun `only an id is remembered, never content`() {
        memory.taskId = "Buy milk and send it to Bob"
        assertNull(memory.taskId)
        memory.taskId = id
        assertEquals(id, memory.taskId)
    }
}
