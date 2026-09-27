package com.hypermind.jarvis.contract

import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The server's rendered task responses, parsed strictly (docs/23 §5.4). */
class TaskSampleTest {
    private val samples =
        ContractJson
            .parseToJsonElement(Shared.read("task_samples.json"))
            .jsonObject
            .getValue("samples")
            .jsonArray
            .associate { s ->
                s.jsonObject
                    .getValue("name")
                    .jsonPrimitive.content to s.jsonObject
            }

    private fun view(name: String): TaskView {
        val sample = samples.getValue(name)
        return TaskView.parse(sample.getValue("http_status").jsonPrimitive.int, sample.getValue("body").toString())
    }

    @Test
    fun `every sample parses into what the device acts on`() {
        assertEquals(TaskStatus.COMPLETED, (view("completed") as TaskView.Result).result.status)
        assertTrue(view("awaiting_confirmation") is TaskView.NeedsConfirmation)
        assertTrue(view("awaiting_step_up") is TaskView.NeedsConfirmation)
        val waiting = (view("waiting_for_platform") as TaskView.Result).result
        assertEquals("shizuku", waiting.waitingFor?.dependency)
        val failed = view("failed_platform") as TaskView.Failed
        assertEquals("platform_unavailable", failed.code)
        val replay = view("replayed_confirmation_without_token") as TaskView.NeedsConfirmation
        assertNull(replay.pending.confirmationToken)
    }

    @Test
    fun `the confirmation screen shows exactly the server's canonical action`() {
        val pending = (view("awaiting_confirmation") as TaskView.NeedsConfirmation).pending
        val screen = ConfirmationText.of(pending)
        assertEquals(
            listOf(
                "Action: app.interact.input_text",
                "In app: com.example.notes",
                "Tool: device.app_interact",
                "text: \"Buy milk\"",
                "view_id: \"note_body\"",
            ),
            screen.lines,
        )
        assertFalse(screen.needsStepUp)
    }

    @Test
    fun `model prose never reaches the confirmation screen`() {
        val sample = samples.getValue("awaiting_confirmation").toString()
        // The paused task carried a note written to steer the user...
        val completedNotes = (view("completed") as TaskView.Result).result.notes
        assertTrue(completedNotes.isNotEmpty())
        val screen = ConfirmationText.of((view("awaiting_confirmation") as TaskView.NeedsConfirmation).pending)
        val shown = (listOf(screen.title) + screen.lines + listOfNotNull(screen.warning)).joinToString("\n")
        assertFalse(shown.contains("Ignore the above"))
        assertFalse(sample.contains("Ignore the above")) // not even sent in the envelope
    }

    @Test
    fun `a high-irreversible action asks for step-up`() {
        val screen = ConfirmationText.of((view("awaiting_step_up") as TaskView.NeedsConfirmation).pending)
        assertTrue(screen.needsStepUp)
        assertTrue(screen.lines.contains("In app: com.wallet.pay"))
        assertTrue(screen.lines.contains("With no arguments"))
    }

    @Test
    fun `an unreadable response is a failure, never a guess`() {
        assertTrue(TaskView.parse(200, "{\"task_id\":\"x\",\"status\":\"done\"}") is TaskView.Failed)
        assertTrue(TaskView.parse(200, "not json") is TaskView.Failed)
        assertTrue(
            TaskView.parse(200, "{\"task_id\":\"x\",\"status\":\"completed\",\"approved\":true}") is TaskView.Failed,
        )
    }
}
