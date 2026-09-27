package com.hypermind.jarvis.tasks

import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.TaskView
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.Instant

/** Tasks from the phone against a mock server replaying the server's own renderings. */
class TaskControllerTest {
    private val server = MockWebServer().apply { start() }
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

    @After
    fun tearDown() = server.shutdown()

    private fun respond(name: String) {
        val sample = samples.getValue(name)
        server.enqueue(
            MockResponse()
                .setResponseCode(sample.getValue("http_status").jsonPrimitive.int)
                .setBody(sample.getValue("body").toString()),
        )
    }

    private var stepUps = 0

    private fun controller(stepUp: StepUpResult = StepUpResult.Attested(Instant.EPOCH)) =
        TaskController(
            api = { ApiClient(OkHttpClient(), server.url("/").toString().trimEnd('/')) },
            accessToken = { "token-1" },
            stepUp = {
                stepUps++
                stepUp
            },
        )

    private fun pendingOf(name: String): TaskView.NeedsConfirmation {
        val sample = samples.getValue(name)
        return TaskView.parse(sample.getValue("http_status").jsonPrimitive.int, sample.getValue("body").toString())
            as TaskView.NeedsConfirmation
    }

    @Test
    fun `a submission carries an idempotency key and nothing about the user`() =
        runBlocking {
            respond("completed")
            val out = controller().submit("add milk to my note")
            assertTrue((out as TaskController.Outcome.Shown).view is TaskView.Result)
            val request = server.takeRequest()
            assertEquals("/api/v1/agent/tasks", request.path)
            assertTrue(!request.getHeader("Idempotency-Key").isNullOrBlank())
            assertEquals(setOf("input"), Json.parseToJsonElement(request.body.readUtf8()).jsonObject.keys)
        }

    @Test
    fun `approving a consequential action posts the server's own token and no step-up`() =
        runBlocking {
            val paused = pendingOf("awaiting_confirmation")
            respond("completed")
            controller().approve(paused.taskId, paused.pending) { it }
            val request = server.takeRequest()
            assertEquals("/api/v1/agent/tasks/${paused.taskId}/confirm", request.path)
            val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
            assertEquals("ct_sample_token_not_real", body.getValue("confirmation_token").jsonPrimitive.content)
            assertEquals("true", body.getValue("approve").jsonPrimitive.content)
            assertEquals(0, stepUps)
        }

    @Test
    fun `a high-irreversible approval re-attests first`() =
        runBlocking {
            val paused = pendingOf("awaiting_step_up")
            respond("completed")
            controller().approve(paused.taskId, paused.pending) { it }
            assertEquals(1, stepUps)
            assertEquals(1, server.requestCount)
        }

    @Test
    fun `if the user does not verify, nothing is sent and the action stays pending`() =
        runBlocking {
            val paused = pendingOf("awaiting_step_up")
            val out = controller(StepUpResult.Cancelled).approve(paused.taskId, paused.pending) { it }
            assertEquals(TaskController.Outcome.StepUpBlocked(StepUpResult.Cancelled), out)
            assertEquals(0, server.requestCount)
        }

    @Test
    fun `a replay without its token fetches it from the task before answering`() =
        runBlocking {
            val paused = pendingOf("replayed_confirmation_without_token")
            // GET /agent/tasks/{id} answers with the live pending action (a plain 200 AgentResult).
            val live =
                samples
                    .getValue("awaiting_confirmation")
                    .getValue("body")
                    .jsonObject
                    .getValue("error")
                    .jsonObject
                    .getValue("details")
                    .jsonObject
                    .getValue("pending")
            server.enqueue(
                MockResponse().setBody(
                    """{"task_id":"${paused.taskId}","status":"awaiting_confirmation","pending":$live}""",
                ),
            )
            respond("completed")
            controller().decline(paused.taskId, paused.pending)
            assertEquals("/api/v1/agent/tasks/${paused.taskId}", server.takeRequest().path)
            val confirm = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
            assertEquals("ct_sample_token_not_real", confirm.getValue("confirmation_token").jsonPrimitive.content)
            assertEquals("false", confirm.getValue("approve").jsonPrimitive.content)
        }
}
