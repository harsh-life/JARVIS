package com.hypermind.jarvis.perception

import com.hypermind.jarvis.contract.ActionResult
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PerceptionCheck
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ResultKind
import com.hypermind.jarvis.contract.ResultStatus
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** The UI execution primitives (docs/23 §5.3, 08 §7) against fake trees. */
class UiActionsTest {
    private val mapping =
        DeviceMapping.load(
            File(requireNotNull(System.getProperty("jarvis.shared.dir"))).resolve("device_mapping.json").readText(),
        )
    private val notes = "com.example.notes"

    private class RecordingDevice : DeviceActions {
        val calls = mutableListOf<String>()
        var accepts = true

        override fun globalAction(name: String): Boolean {
            calls += "global:$name"
            return accepts
        }

        override fun launch(packageName: String): Boolean {
            calls += "launch:$packageName"
            return accepts
        }
    }

    private fun envelope(
        operation: String,
        arguments: Map<String, Any> = emptyMap(),
        capability: String = "app.interact",
    ) = OperationEnvelope(
        opId = "op",
        taskId = "t",
        deviceId = "d",
        capability = capability,
        operation = operation,
        primitive = mapping.lookup(capability, operation)!!.primitive,
        packageName = notes,
        arguments =
            JsonObject(
                arguments.mapValues { (_, v) -> if (v is Int) JsonPrimitive(v) else JsonPrimitive(v.toString()) },
            ),
        mappingVersion = mapping.version,
        issuedAt = "2026-09-26T12:00:00Z",
        expiresAt = "2026-09-26T12:00:30Z",
    )

    private fun screen(
        root: FakeNode,
        pkg: String = notes,
    ) = FakeScreen(ForegroundWindow(pkg, null, null, root))

    private fun target(result: ResultEnvelope) =
        ContractJson
            .decodeFromJsonElement(
                ActionResult.serializer(),
                result.result!!,
            ).target

    private fun assertAcceptable(result: ResultEnvelope) =
        assertNull(PerceptionCheck.problem(ResultKind.ACTION, result.result!!, result.perceptionLevel, notes))

    @Test
    fun `tap clicks the nearest clickable ancestor of the matched label`() {
        val label = FakeNode(text = "Save")
        val row = FakeNode(className = "android.widget.LinearLayout", clickable = true, children = listOf(label))
        val out = UiActions(screen(layout(row)), RecordingDevice()).tap(envelope("tap", mapOf("text" to "Save")))
        assertEquals(ResultStatus.OK, out.status)
        assertEquals(listOf<NodeAction>(NodeAction.Click), row.performed)
        assertTrue(label.performed.isEmpty())
        assertEquals("android.widget.LinearLayout", target(out)?.role)
        assertAcceptable(out)
    }

    @Test
    fun `tap never acts outside the named app`() {
        val button = FakeNode(text = "Send", clickable = true)
        val out =
            UiActions(screen(layout(button), pkg = "com.bank.app"), RecordingDevice()).tap(
                envelope(
                    "tap",
                    mapOf(
                        "text" to "Send",
                    ),
                ),
            )
        assertEquals(RefusalReason.PACKAGE_MISMATCH, out.refusalReason)
        assertTrue(button.performed.isEmpty())
    }

    @Test
    fun `an absent or unclickable target is an observation, not a guess`() {
        val lonely = FakeNode(text = "Save")
        val ui = UiActions(screen(layout(lonely)), RecordingDevice())
        assertEquals(FailureReason.TARGET_NOT_FOUND, ui.tap(envelope("tap", mapOf("text" to "Send"))).failureReason)
        assertEquals(FailureReason.TARGET_NOT_FOUND, ui.tap(envelope("tap", mapOf("text" to "Save"))).failureReason)
        assertTrue(lonely.performed.isEmpty())
    }

    @Test
    fun `a disabled target is not clicked`() {
        val button = FakeNode(text = "Pay", clickable = true, enabled = false)
        val out = UiActions(screen(layout(button)), RecordingDevice()).tap(envelope("tap", mapOf("text" to "Pay")))
        assertEquals(FailureReason.ACTION_FAILED, out.failureReason)
        assertTrue(button.performed.isEmpty())
    }

    @Test
    fun `the index picks among several matches`() {
        val first = FakeNode(text = "Delete", clickable = true)
        val second = FakeNode(text = "Delete", clickable = true)
        UiActions(screen(layout(first, second)), RecordingDevice()).tap(
            envelope(
                "tap",
                mapOf(
                    "text" to "Delete",
                    "index" to 1,
                ),
            ),
        )
        assertTrue(first.performed.isEmpty())
        assertEquals(1, second.performed.size)
    }

    @Test
    fun `swipe scrolls the named or first scrollable container`() {
        val list =
            FakeNode(className = "androidx.recyclerview.widget.RecyclerView", scrollable = true, viewId = "app:id/list")
        val ui = UiActions(screen(layout(FakeNode(text = "x"), list)), RecordingDevice())
        assertEquals(ResultStatus.OK, ui.swipe(envelope("swipe", mapOf("direction" to "down"))).status)
        assertEquals(
            ResultStatus.OK,
            ui.swipe(envelope("swipe", mapOf("direction" to "up", "view_id" to "list"))).status,
        )
        assertEquals(listOf<NodeAction>(NodeAction.Scroll("down"), NodeAction.Scroll("up")), list.performed)
    }

    @Test
    fun `input_text sets text on the named or focused editable field`() {
        val field = FakeNode(className = "android.widget.EditText", editable = true, focused = true)
        val out =
            UiActions(screen(layout(field)), RecordingDevice()).inputText(
                envelope(
                    "input_text",
                    mapOf(
                        "text" to "milk",
                    ),
                ),
            )
        assertEquals(ResultStatus.OK, out.status)
        assertEquals(listOf<NodeAction>(NodeAction.SetText("milk")), field.performed)
        assertAcceptable(out)
    }

    @Test
    fun `text is never typed into a password field`() {
        val pin =
            FakeNode(className = "android.widget.EditText", editable = true, password = true, viewId = "app:id/pin")
        val out =
            UiActions(screen(layout(pin)), RecordingDevice()).inputText(
                envelope(
                    "input_text",
                    mapOf(
                        "text" to "1234",
                        "view_id" to "pin",
                    ),
                ),
            )
        assertEquals(FailureReason.ACTION_FAILED, out.failureReason)
        assertTrue(pin.performed.isEmpty())
    }

    @Test
    fun `input_text with no named and no focused field fails`() {
        val field = FakeNode(className = "android.widget.EditText", editable = true)
        val out =
            UiActions(screen(layout(field)), RecordingDevice()).inputText(
                envelope(
                    "input_text",
                    mapOf(
                        "text" to "x",
                    ),
                ),
            )
        assertEquals(FailureReason.TARGET_NOT_FOUND, out.failureReason)
    }

    @Test
    fun `a node that rejects the action is an explicit failure`() {
        val button = FakeNode(text = "Save", clickable = true, accepts = false)
        val out = UiActions(screen(layout(button)), RecordingDevice()).tap(envelope("tap", mapOf("text" to "Save")))
        assertEquals(FailureReason.ACTION_FAILED, out.failureReason)
    }

    @Test
    fun `global actions run only with the named app in front`() {
        val device = RecordingDevice()
        val ui = UiActions(screen(layout()), device)
        val back = envelope("global_action", mapOf("action" to "back"), capability = "device.ui_control")
        assertEquals(ResultStatus.OK, ui.globalAction(back).status)
        val elsewhere = UiActions(screen(layout(), pkg = "com.bank.app"), device)
        assertEquals(RefusalReason.PACKAGE_MISMATCH, elsewhere.globalAction(back).refusalReason)
        assertEquals(listOf("global:back"), device.calls)
    }

    @Test
    fun `launch opens exactly the named app's launcher entry`() {
        val device = RecordingDevice()
        val out = UiActions(screen(layout(), pkg = "com.other.app"), device).launch(envelope("launch_activity"))
        assertEquals(ResultStatus.OK, out.status)
        assertEquals(listOf("launch:$notes"), device.calls)
        device.accepts = false
        assertEquals(
            FailureReason.ACTION_FAILED,
            UiActions(screen(layout()), device).launch(envelope("launch_activity")).failureReason,
        )
    }
}
