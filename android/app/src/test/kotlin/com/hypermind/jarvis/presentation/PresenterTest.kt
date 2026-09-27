package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.channel.ReconnectCause
import com.hypermind.jarvis.contract.AgentResult
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.PlatformWait
import com.hypermind.jarvis.contract.RiskCategory
import com.hypermind.jarvis.contract.TaskFailure
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.Instant

/**
 * The one mapping, canonical state → PresentationState (docs/23 §7), against
 * the server's own renderings (`shared/android/task_samples.json`).
 */
class PresenterTest {
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

    private fun present(
        task: TaskSnapshot = TaskSnapshot.Idle,
        channel: ChannelState = connected,
        enrolled: Boolean = true,
        platforms: Map<PlatformDependency, Boolean> = emptyMap(),
        operations: List<ActiveOperation> = emptyList(),
        level: PerceptionLevel? = null,
        voice: VoiceActivity = VoiceActivity.NONE,
    ) = Presenter.of(PresentationInputs(enrolled, channel, task, platforms, operations, level, voice = voice))

    private fun known(result: AgentResult) = TaskSnapshot.Known(TaskView.Result(result))

    private val read = ActiveOperation("t", "device.read", "read_screen", "accessibility.read_tree")

    // ── every task state ───────────────────────────────────────────────

    @Test
    fun `idle, listening, submitting, thinking and executing`() {
        assertEquals(TaskPhase.IDLE, present().taskStatus)
        assertEquals(TaskPhase.LISTENING, present(voice = VoiceActivity.LISTENING).taskStatus)
        assertEquals(TaskPhase.SUBMITTING, present(TaskSnapshot.Submitting).taskStatus)
        assertEquals(TaskPhase.THINKING, present(TaskSnapshot.InFlight).taskStatus)
        val executing = present(TaskSnapshot.InFlight, operations = listOf(read))
        assertEquals(TaskPhase.EXECUTING, executing.taskStatus)
        assertEquals(DeviceActivity.READING_SCREEN, executing.deviceContext.activity)
        // A running task the server reports (e.g. after a resume) is working too.
        assertEquals(TaskPhase.THINKING, present(known(AgentResult("t", TaskStatus.RUNNING))).taskStatus)
    }

    @Test
    fun `completed, cancelled and failed come from the server's answer`() {
        assertEquals(TaskPhase.COMPLETED, present(TaskSnapshot.Known(sample("completed"))).taskStatus)
        assertEquals(TaskPhase.CANCELLED, present(known(AgentResult("t", TaskStatus.CANCELLED))).taskStatus)
        val failed = present(TaskSnapshot.Known(sample("failed_platform")))
        assertEquals(TaskPhase.FAILED, failed.taskStatus)
        assertEquals(ErrorKind.PLATFORM_UNAVAILABLE, failed.error?.kind)
    }

    @Test
    fun `offline and reconnecting when nothing is running`() {
        assertEquals(TaskPhase.OFFLINE, present(channel = ChannelState.Stopped).taskStatus)
        assertEquals(TaskPhase.OFFLINE, present(enrolled = false, channel = ChannelState.Stopped).taskStatus)
        assertEquals(TaskPhase.RECONNECTING, present(channel = ChannelState.Connecting).taskStatus)
        val lost = ChannelState.Reconnecting(1, 1000, "connection lost")
        assertEquals(TaskPhase.RECONNECTING, present(channel = lost).taskStatus)
        val expired = ChannelState.Reconnecting(1, 1000, "x", ReconnectCause.AUTHENTICATION_EXPIRED)
        val auth = present(channel = expired)
        assertEquals(TaskPhase.RECONNECTING, auth.taskStatus)
        assertEquals(ErrorKind.AUTH_EXPIRED, auth.error?.kind)
        assertEquals(ConnectionPhase.AUTH_EXPIRED, auth.deviceContext.connection)
        assertEquals(ErrorKind.CHANNEL_DISABLED, present(channel = ChannelState.Disabled).error?.kind)
    }

    @Test
    fun `a server-side task keeps its state while the phone reconnects`() {
        val lost = ChannelState.Reconnecting(1, 1000, "connection lost")
        val state = present(TaskSnapshot.Known(sample("awaiting_confirmation")), channel = lost)
        assertEquals(TaskPhase.WAITING_FOR_CONFIRMATION, state.taskStatus)
        assertEquals(ConnectionPhase.RECONNECTING, state.deviceContext.connection)
    }

    @Test
    fun `revoked is blocked whatever else is known, and update required is blocked`() {
        val revoked = present(TaskSnapshot.Known(sample("awaiting_confirmation")), channel = ChannelState.Revoked)
        assertEquals(TaskPhase.BLOCKED, revoked.taskStatus)
        assertEquals(ErrorKind.REVOKED, revoked.error?.kind)
        assertFalse(revoked.canCancel)
        val update = present(channel = ChannelState.UpdateRequired("1-ffff"))
        assertEquals(TaskPhase.BLOCKED, update.taskStatus)
        assertEquals(ErrorKind.UPDATE_REQUIRED, update.error?.kind)
    }

    // ── confirmation and step-up ───────────────────────────────────────

    @Test
    fun `a pending action is a confirmation state with its server-assigned tier`() {
        val state = present(TaskSnapshot.Known(sample("awaiting_confirmation")))
        assertEquals(TaskPhase.WAITING_FOR_CONFIRMATION, state.taskStatus)
        assertEquals(RiskCategory.CONSEQUENTIAL, state.riskTierPending)
        assertFalse(state.stepUpRequired)
        assertTrue(state.canCancel)
        val stepUp = present(TaskSnapshot.Known(sample("awaiting_step_up")))
        assertEquals(RiskCategory.HIGH_IRREVERSIBLE, stepUp.riskTierPending)
        assertTrue(stepUp.stepUpRequired)
    }

    @Test
    fun `a blocked step-up keeps the action pending and says why`() {
        val pending = (sample("awaiting_step_up") as TaskView.NeedsConfirmation).pending
        for ((result, kind) in listOf(
            StepUpResult.Cancelled to ErrorKind.STEP_UP_CANCELLED,
            StepUpResult.NoKey to ErrorKind.STEP_UP_UNAVAILABLE,
            StepUpResult.Failed to ErrorKind.STEP_UP_FAILED,
        )) {
            val state = present(TaskSnapshot.StepUpBlocked("t", pending, result))
            assertEquals(TaskPhase.WAITING_FOR_CONFIRMATION, state.taskStatus)
            assertEquals(kind, state.error?.kind)
            assertTrue(state.stepUpRequired)
            assertTrue(
                PresentationText.of(state).detail!!.contains("Nothing was approved") ||
                    PresentationText.of(state).detail!!.contains("nothing was approved"),
            )
        }
    }

    // ── waiting for a platform dependency ──────────────────────────────

    @Test
    fun `waiting for Shizuku says what is missing, why, that the task waits, and what to do`() {
        val state = present(TaskSnapshot.Known(sample("waiting_for_platform")))
        assertEquals(TaskPhase.WAITING_FOR_PLATFORM, state.taskStatus)
        assertEquals(WaitDependency.SHIZUKU, state.waitingFor?.dependency)
        assertNotNull(state.waitingFor?.until)
        assertTrue(state.canCancel)
        val words = PresentationText.of(state)
        assertEquals("Waiting for Shizuku", words.headline)
        assertTrue(words.detail!!.contains("paused"))
        assertEquals(PresentationText.UserAction.OPEN_SHIZUKU, words.action)
    }

    @Test
    fun `every dependency has its own wording, and none says anything is queued`() {
        for (dep in listOf(
            "accessibility_service",
            "shizuku",
            "notification_access",
            "screen_capture",
            "ocr",
            "device_channel",
            "something_new",
        )) {
            val result =
                AgentResult(
                    "t",
                    TaskStatus.WAITING_FOR_PLATFORM,
                    waitingFor = PlatformWait(dep, "d", "2099-01-01T00:00:00Z"),
                )
            val words = PresentationText.of(present(known(result)))
            assertTrue(words.headline, words.headline.startsWith("Waiting for"))
            val all = (words.headline + " " + words.detail).lowercase()
            assertFalse(dep, "queue" in all)
            assertTrue(dep, "checks everything again" in all)
        }
        val access =
            PresentationText.of(
                present(
                    known(
                        AgentResult(
                            "t",
                            TaskStatus.WAITING_FOR_PLATFORM,
                            waitingFor = PlatformWait("accessibility_service", "d", "x"),
                        ),
                    ),
                ),
            )
        assertEquals("Waiting for Accessibility access", access.headline)
        assertEquals(PresentationText.UserAction.OPEN_ACCESSIBILITY_SETTINGS, access.action)
        val notif =
            PresentationText.of(
                present(
                    known(
                        AgentResult(
                            "t",
                            TaskStatus.WAITING_FOR_PLATFORM,
                            waitingFor = PlatformWait("notification_access", "d", "x"),
                        ),
                    ),
                ),
            )
        assertEquals("Waiting for notification access", notif.headline)
    }

    @Test
    fun `the wait shows when the dependency is back on this phone (the server resumes by itself)`() {
        val result = (sample("waiting_for_platform") as TaskView.Result).result
        val back = present(known(result), platforms = mapOf(PlatformDependency.SHIZUKU to true))
        assertEquals(true, back.waitingFor?.availableNow)
        assertEquals(TaskPhase.WAITING_FOR_PLATFORM, back.taskStatus) // still the server's state
        val gone = present(known(result), platforms = mapOf(PlatformDependency.SHIZUKU to false))
        assertEquals(false, gone.waitingFor?.availableNow)
    }

    // ── errors ─────────────────────────────────────────────────────────

    @Test
    fun `server codes map to kinds, refusals are blocked, and nothing unknown becomes success`() {
        val expected =
            mapOf(
                "unauthorized" to (ErrorKind.REFUSED to TaskPhase.BLOCKED),
                "prohibited" to (ErrorKind.REFUSED to TaskPhase.BLOCKED),
                "principal_revoked" to (ErrorKind.REVOKED to TaskPhase.BLOCKED),
                "emergency_stop" to (ErrorKind.STOPPED_BY_OPERATOR to TaskPhase.BLOCKED),
                "rate_limited" to (ErrorKind.LIMIT_REACHED to TaskPhase.FAILED),
                "budget_exceeded" to (ErrorKind.LIMIT_REACHED to TaskPhase.FAILED),
                "model_unavailable" to (ErrorKind.SERVER_UNAVAILABLE to TaskPhase.FAILED),
                "confirmation_expired" to (ErrorKind.CONFIRMATION_EXPIRED to TaskPhase.FAILED),
                "platform_unavailable" to (ErrorKind.PLATFORM_UNAVAILABLE to TaskPhase.FAILED),
                "device_unavailable" to (ErrorKind.DEVICE_UNAVAILABLE to TaskPhase.FAILED),
                "operation_expired" to (ErrorKind.OPERATION_EXPIRED to TaskPhase.FAILED),
                "token_expired" to (ErrorKind.AUTH_EXPIRED to TaskPhase.FAILED),
                "max_iterations_exceeded" to (ErrorKind.TASK_FAILED to TaskPhase.FAILED),
                "a_code_from_the_future" to (ErrorKind.TASK_FAILED to TaskPhase.FAILED),
            )
        for ((code, pair) in expected) {
            val viaFailure = present(known(AgentResult("t", TaskStatus.FAILED, failure = TaskFailure(code, "m"))))
            assertEquals(code, pair.first, viaFailure.error?.kind)
            assertEquals(code, pair.second, viaFailure.taskStatus)
            val viaEnvelope = present(TaskSnapshot.Known(TaskView.Failed("t", code, "m")))
            assertEquals(code, pair.first, viaEnvelope.error?.kind)
        }
        // A malformed answer is an error, never a result.
        val malformed = present(TaskSnapshot.Known(TaskView.parse(200, "{\"status\":\"done\"}")))
        assertEquals(TaskPhase.FAILED, malformed.taskStatus)
        assertEquals(ErrorKind.INVALID_RESPONSE, malformed.error?.kind)
        // Awaiting confirmation without a pending action is not a confirmation screen.
        val empty = present(known(AgentResult("t", TaskStatus.AWAITING_CONFIRMATION)))
        assertEquals(ErrorKind.INVALID_RESPONSE, empty.error?.kind)
    }

    @Test
    fun `an unreachable server offers the safe retry`() {
        val state = present(TaskSnapshot.Unreachable)
        assertEquals(ErrorKind.SERVER_UNREACHABLE, state.error?.kind)
        assertTrue(state.canRetry)
        assertEquals(TaskPhase.OFFLINE, present(TaskSnapshot.Unreachable, channel = ChannelState.Stopped).taskStatus)
    }

    // ── device activity (perception status) ────────────────────────────

    @Test
    fun `the perception rung and the kind of action show, never their content`() {
        fun op(primitive: String) = ActiveOperation("t", "c", "o", primitive)
        assertEquals(DeviceActivity.USING_OCR, Presenter.activity(listOf(read), PerceptionLevel.OCR))
        assertEquals(
            DeviceActivity.READING_CONTROLS,
            Presenter.activity(listOf(op("accessibility.read_element")), null),
        )
        assertEquals(DeviceActivity.VISUAL_FALLBACK, Presenter.activity(listOf(op("accessibility.screenshot")), null))
        assertEquals(DeviceActivity.ACTING_IN_APP, Presenter.activity(listOf(op("accessibility.tap")), null))
        assertEquals(
            DeviceActivity.PRIVILEGED_ACTION,
            Presenter.activity(listOf(op("shizuku.force_stop_package")), null),
        )
        assertEquals(
            DeviceActivity.READING_NOTIFICATIONS,
            Presenter.activity(listOf(op("android.api.notification_query")), null),
        )
        assertNull(Presenter.activity(emptyList(), PerceptionLevel.OCR))
    }

    // ── the boundary ───────────────────────────────────────────────────

    @Test
    fun `presentation state carries no task content, prose, token or id`() {
        val confirm = sample("awaiting_confirmation") as TaskView.NeedsConfirmation
        val states =
            listOf(
                present(TaskSnapshot.Known(confirm)),
                present(TaskSnapshot.Known(sample("completed"))),
                present(TaskSnapshot.StepUpBlocked(confirm.taskId, confirm.pending, StepUpResult.Cancelled)),
            )
        val secrets =
            listOf(
                "Buy milk",
                "note_body",
                "ct_sample_token_not_real",
                "Ignore the above",
                "Added 'Buy milk'",
                confirm.taskId,
                "com.example.notes",
            )
        for (state in states) {
            val rendered = state.toString() + PresentationText.of(state) + PresentationSignal.of(state)
            secrets.forEach { assertFalse("$it leaked", rendered.contains(it)) }
        }
    }

    @Test
    fun `every phase has wording and a signal, and attention states stand out`() {
        for (phase in TaskPhase.entries) {
            val state = PresentationState(phase, deviceContext = DeviceContext(ConnectionPhase.CONNECTED))
            assertTrue(phase.name, PresentationText.of(state).headline.isNotBlank())
            PresentationSignal.of(state)
        }
        val attention = listOf(TaskPhase.WAITING_FOR_CONFIRMATION, TaskPhase.WAITING_FOR_PLATFORM)
        attention.forEach {
            assertTrue(
                PresentationSignal
                    .of(
                        PresentationState(it, deviceContext = DeviceContext(ConnectionPhase.CONNECTED)),
                    ).emphasis,
            )
        }
    }

    @Test
    fun `the mapping is deterministic`() {
        val inputs = PresentationInputs(true, connected, TaskSnapshot.Known(sample("waiting_for_platform")))
        assertEquals(Presenter.of(inputs), Presenter.of(inputs))
    }
}
