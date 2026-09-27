package com.hypermind.jarvis.ui

import android.view.View
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.contract.AgentResult
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.RiskCategory
import com.hypermind.jarvis.contract.TaskFailure
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.presentation.PresentationInputs
import com.hypermind.jarvis.presentation.Presenter
import com.hypermind.jarvis.presentation.TaskSnapshot
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.time.Instant

/** What the task panel shows, chosen from the server's answer and the one presentation state. */
@RunWith(RobolectricTestRunner::class)
class TaskPanelTest {
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

    private val connected = ChannelState.Connected(Instant.parse("2099-01-01T00:00:00Z"))

    private fun content(
        snapshot: TaskSnapshot,
        channel: ChannelState = connected,
    ) = PanelContent.of(snapshot, Presenter.of(PresentationInputs(true, channel, snapshot)))

    @Test
    fun `a completed task shows the server's answer`() {
        assertEquals(
            PanelContent.Answer("Added 'Buy milk' to your note."),
            content(TaskSnapshot.Known(sample("completed"))),
        )
    }

    @Test
    fun `a pending action shows only the canonical confirmation, never the task's notes`() {
        val confirm = content(TaskSnapshot.Known(sample("awaiting_confirmation"))) as PanelContent.Confirm
        assertEquals("app.interact", confirm.pending.capability)
        assertFalse(confirm.toString().contains("Ignore the above"))
        // A pending action read back from GET (a Result) is the same card.
        val paused = sample("awaiting_confirmation") as TaskView.NeedsConfirmation
        val viaGet =
            TaskView.Result(
                AgentResult(paused.taskId, TaskStatus.AWAITING_CONFIRMATION, pending = paused.pending),
            )
        assertEquals(confirm, content(TaskSnapshot.Known(viaGet)))
    }

    @Test
    fun `a blocked step-up keeps the card up (the action is still pending)`() {
        val paused = sample("awaiting_step_up") as TaskView.NeedsConfirmation
        val shown = content(TaskSnapshot.StepUpBlocked(paused.taskId, paused.pending, StepUpResult.Cancelled))
        assertEquals(PanelContent.Confirm(paused.taskId, paused.pending), shown)
    }

    @Test
    fun `a failure shows the server's own message, a waiting task shows nothing extra`() {
        val failed =
            TaskView.Result(
                AgentResult("t", TaskStatus.FAILED, failure = TaskFailure("stalled", "The task stopped.")),
            )
        assertEquals(PanelContent.ServerMessage("The task stopped."), content(TaskSnapshot.Known(failed)))
        assertEquals(PanelContent.Nothing, content(TaskSnapshot.Known(sample("waiting_for_platform"))))
        assertEquals(PanelContent.Nothing, content(TaskSnapshot.InFlight))
    }

    @Test
    fun `a revoked phone shows nothing it could act on`() {
        val shown = content(TaskSnapshot.Known(sample("awaiting_confirmation")), channel = ChannelState.Revoked)
        assertEquals(PanelContent.Nothing, shown)
    }

    @Test
    fun `the risk tier and step-up are stated on the card`() {
        assertEquals("Risk: cannot be undone", riskLabel(RiskCategory.HIGH_IRREVERSIBLE))
        assertEquals("Risk: acts on your behalf", riskLabel(RiskCategory.CONSEQUENTIAL))
        assertTrue(RiskCategory.entries.all { riskLabel(it).startsWith("Risk:") })
    }

    @Test
    fun `the approval screen drops touches while another window obscures it`() {
        val view = View(ApplicationProvider.getApplicationContext())
        assertFalse(view.filterTouchesWhenObscured)
        SecureTouch.protect(view)
        assertTrue(view.filterTouchesWhenObscured)
    }
}
