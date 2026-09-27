package com.hypermind.jarvis.ui

import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.contract.AgentResult
import com.hypermind.jarvis.contract.PlatformWait
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.tasks.TaskController
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TaskPanelStateTest {
    private fun shown(result: AgentResult) = TaskPanelState.of(TaskController.Outcome.Shown(TaskView.Result(result)))

    @Test
    fun `a waiting task tells the user what to turn on`() {
        val state =
            shown(AgentResult("t", TaskStatus.WAITING_FOR_PLATFORM, waitingFor = PlatformWait("shizuku", "d", "x")))
        assertTrue((state as TaskPanelState.Message).text.contains("shizuku"))
    }

    @Test
    fun `a task waiting for its device to reconnect says so plainly`() {
        val state =
            shown(
                AgentResult(
                    "t",
                    TaskStatus.WAITING_FOR_PLATFORM,
                    waitingFor = PlatformWait("device_channel", "d", "x"),
                ),
            )
        val text = (state as TaskPanelState.Message).text
        assertTrue(text.contains("reconnect"))
        assertTrue(!text.contains("device_channel"))
    }

    @Test
    fun `a blocked step-up says plainly that nothing was approved`() {
        for (result in listOf(StepUpResult.Cancelled, StepUpResult.NoKey, StepUpResult.Failed)) {
            val state = TaskPanelState.of(TaskController.Outcome.StepUpBlocked(result))
            assertTrue((state as TaskPanelState.Message).text.contains("Nothing was approved"))
        }
    }

    @Test
    fun `a completed task shows its answer`() {
        assertEquals(TaskPanelState.Answer("done"), shown(AgentResult("t", TaskStatus.COMPLETED, response = "done")))
    }
}
