package com.hypermind.jarvis.perception

import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PerceptionCheck
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ResultStatus
import com.hypermind.jarvis.contract.ScreenNode
import com.hypermind.jarvis.contract.ScreenReadResult
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** The perception ladder (docs/23 §6), against fake screens. */
class ScreenPerceptionTest {
    private val mapping =
        DeviceMapping.load(
            File(requireNotNull(System.getProperty("jarvis.shared.dir"))).resolve("device_mapping.json").readText(),
        )
    private val readTreeSpec: PrimitiveSpec = mapping.lookup("device.read", "read_screen")!!
    private val readElementSpec: PrimitiveSpec = mapping.lookup("app.interact", "read_screen_element")!!
    private val notes = "com.example.notes"

    private fun envelope(
        operation: String = "read_screen",
        arguments: Map<String, String> = emptyMap(),
    ) = OperationEnvelope(
        opId = "op",
        taskId = "t",
        deviceId = "d",
        capability = if (operation == "read_screen") "device.read" else "app.interact",
        operation = operation,
        primitive = if (operation == "read_screen") "accessibility.read_tree" else "accessibility.read_element",
        packageName = notes,
        arguments = JsonObject(arguments.mapValues { JsonPrimitive(it.value) }),
        mappingVersion = mapping.version,
        issuedAt = "2026-09-26T12:00:00Z",
        expiresAt = "2026-09-26T12:00:30Z",
    )

    private class CountingOcr(
        private val texts: List<OcrText>?,
        private val after: () -> Unit = {},
    ) : ScreenOcr {
        var calls = 0

        override suspend fun readScreen(): List<OcrText>? {
            calls++
            after()
            return texts
        }
    }

    private fun window(
        root: A11yNode?,
        pkg: String = notes,
    ) = ForegroundWindow(pkg, "$pkg.MainActivity", "Notes", root)

    private fun result(envelope: ResultEnvelope) =
        ContractJson.decodeFromJsonElement(
            ScreenReadResult.serializer(),
            envelope.result!!,
        )

    private fun assertAcceptable(
        envelope: ResultEnvelope,
        spec: PrimitiveSpec = readTreeSpec,
    ) = assertNull(PerceptionCheck.problem(spec.result, envelope.result!!, envelope.perceptionLevel, notes))

    @Test
    fun `a readable tree stops the ladder at accessibility and never takes a frame`() =
        runBlocking {
            val ocr = CountingOcr(listOf(OcrText("x", listOf(0, 0, 1, 1))))
            val out =
                ScreenPerception(
                    FakeScreen(window(layout(FakeNode(text = "Hello")))),
                    ocr,
                ).readTree(envelope(), readTreeSpec)
            assertEquals(PerceptionLevel.ACCESSIBILITY, out.perceptionLevel)
            assertEquals(0, ocr.calls)
            assertEquals("Notes", result(out).app.windowTitle)
            assertAcceptable(out)
        }

    @Test
    fun `an unreadable tree climbs to on-device OCR, minus anything inside a password field`() =
        runBlocking {
            val tree =
                layout(
                    FakeNode(className = "android.widget.EditText", password = true, bounds = listOf(0, 100, 500, 150)),
                )
            val ocr =
                CountingOcr(
                    listOf(
                        OcrText("Balance 42", listOf(0, 0, 500, 50)),
                        OcrText("hunter2", listOf(10, 110, 200, 140)),
                    ),
                )
            val out = ScreenPerception(FakeScreen(window(tree)), ocr).readTree(envelope(), readTreeSpec)
            assertEquals(PerceptionLevel.OCR, out.perceptionLevel)
            assertEquals(listOf("Balance 42"), result(out).ocrBlocks.map { it.text })
            assertAcceptable(out)
        }

    @Test
    fun `with nothing readable and no OCR only app metadata is returned`() =
        runBlocking {
            val out =
                ScreenPerception(
                    FakeScreen(window(layout())),
                    CountingOcr(null),
                ).readTree(envelope(), readTreeSpec)
            assertEquals(PerceptionLevel.APP_METADATA, out.perceptionLevel)
            assertTrue(result(out).nodes.isEmpty())
            assertEquals(notes, result(out).app.packageName)
            assertAcceptable(out)
        }

    @Test
    fun `another app in front is refused, never read instead`() =
        runBlocking {
            val ocr = CountingOcr(listOf(OcrText("x", listOf(0, 0, 1, 1))))
            val out =
                ScreenPerception(FakeScreen(window(layout(FakeNode(text = "Balance")), pkg = "com.bank.app")), ocr)
                    .readTree(envelope(), readTreeSpec)
            assertEquals(RefusalReason.PACKAGE_MISMATCH, out.refusalReason)
            assertNull(out.result)
            assertEquals(0, ocr.calls)
        }

    @Test
    fun `an app switch during OCR discards the frame's text`() =
        runBlocking {
            val screen = FakeScreen(window(layout()))
            val ocr =
                CountingOcr(listOf(OcrText("Balance 42", listOf(0, 0, 1, 1)))) {
                    screen.window =
                        window(layout(), pkg = "com.bank.app")
                }
            val out = ScreenPerception(screen, ocr).readTree(envelope(), readTreeSpec)
            assertEquals(RefusalReason.PACKAGE_MISMATCH, out.refusalReason)
            assertNull(out.result)
        }

    @Test
    fun `no active window is an explicit failure`() =
        runBlocking {
            val out = ScreenPerception(FakeScreen(null), null).readTree(envelope(), readTreeSpec)
            assertEquals(ResultStatus.FAILED, out.status)
        }

    @Test
    fun `read_element returns the target's subtree re-rooted`() =
        runBlocking {
            val tree =
                layout(
                    FakeNode(text = "Header"),
                    FakeNode(
                        className = "android.widget.LinearLayout",
                        viewId = "com.example.notes:id/row",
                        children = listOf(FakeNode(text = "Buy milk"), FakeNode(text = "Done", clickable = true)),
                    ),
                )
            val out =
                ScreenPerception(FakeScreen(window(tree)), null)
                    .readElement(envelope("read_screen_element", mapOf("view_id" to "row")), readElementSpec)
            val nodes = result(out).nodes
            assertEquals(listOf(null, 0, 0), nodes.map { it.parent })
            assertEquals(listOf(null, "Buy milk", "Done"), nodes.map { it.text })
            assertAcceptable(out, readElementSpec)
        }

    @Test
    fun `an absent target fails as an observation`() =
        runBlocking {
            val out =
                ScreenPerception(FakeScreen(window(layout(FakeNode(text = "Header")))), null)
                    .readElement(envelope("read_screen_element", mapOf("text" to "Send")), readElementSpec)
            assertEquals(FailureReason.TARGET_NOT_FOUND, out.failureReason)
        }

    @Test
    fun `a password node never matches by text`() {
        val nodes =
            listOf(
                ScreenNode(id = 0, role = "r", bounds = listOf(0, 0, 1, 1)),
                ScreenNode(id = 1, parent = 0, role = "r", bounds = listOf(0, 0, 1, 1), password = true),
            )
        assertNull(Selector(text = "hunter2").find(nodes))
    }

    @Test
    fun `an oversized screen is trimmed from the end, stays a valid tree, and says so`() =
        runBlocking {
            val tree = layout(*Array(299) { FakeNode(text = "x".repeat(400)) })
            val out = ScreenPerception(FakeScreen(window(tree)), null).readTree(envelope(), readTreeSpec)
            assertEquals(ResultStatus.OK, out.status)
            val fitted = result(out)
            assertTrue(fitted.truncated)
            assertTrue(fitted.nodes.size < 300)
            assertTrue(
                ContractJson.encodeToString(ResultEnvelope.serializer(), out).toByteArray().size <=
                    readTreeSpec.maxResultBytes,
            )
            assertAcceptable(out)
            assertFalse(fitted.nodes.isEmpty())
        }
}
