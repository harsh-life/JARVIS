package com.hypermind.jarvis

import com.hypermind.jarvis.contract.AgentResult
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.presentation.TaskSnapshot
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** OD-CHAR-2: only a fresh, server-confirmed completion reaches the character. */
class CharacterEventsTest {
    private fun result(
        id: String,
        status: TaskStatus,
    ) = TaskSnapshot.Known(TaskView.Result(AgentResult(taskId = id, status = status)))

    @Test
    fun `a task that goes from running to completed is a fresh completion`() {
        assertTrue(CharacterEvents.freshCompletion(TaskSnapshot.InFlight, result("t1", TaskStatus.COMPLETED)))
        assertTrue(
            CharacterEvents.freshCompletion(result("t1", TaskStatus.RUNNING), result("t1", TaskStatus.COMPLETED)),
        )
    }

    @Test
    fun `failure, cancellation and repeats are not completions`() {
        assertFalse(CharacterEvents.freshCompletion(TaskSnapshot.InFlight, result("t1", TaskStatus.FAILED)))
        assertFalse(CharacterEvents.freshCompletion(TaskSnapshot.InFlight, result("t1", TaskStatus.CANCELLED)))
        assertFalse(
            CharacterEvents.freshCompletion(result("t1", TaskStatus.COMPLETED), result("t1", TaskStatus.COMPLETED)),
        )
        assertFalse(CharacterEvents.freshCompletion(TaskSnapshot.InFlight, TaskSnapshot.Unreachable))
    }

    @Test
    fun `re-attaching to an already finished task is not a fresh completion`() {
        assertFalse(CharacterEvents.freshCompletion(null, result("t1", TaskStatus.COMPLETED)))
    }

    @Test
    fun `completions are forwarded once each`() =
        runTest {
            var count = 0
            val snapshots =
                flowOf(
                    result("t0", TaskStatus.COMPLETED), // restored on launch: not counted
                    TaskSnapshot.Submitting,
                    TaskSnapshot.InFlight,
                    result("t1", TaskStatus.COMPLETED),
                    result("t1", TaskStatus.COMPLETED),
                    TaskSnapshot.Submitting,
                    result("t2", TaskStatus.FAILED),
                    TaskSnapshot.InFlight,
                    result("t3", TaskStatus.COMPLETED),
                )
            CharacterEvents.forwardCompletions(snapshots) { count++ }
            assertEquals(2, count)
        }
}
