package com.hypermind.jarvis.action

import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.DeviceGuard
import com.hypermind.jarvis.contract.DeviceLocalState
import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.GridState
import com.hypermind.jarvis.contract.GridToggle
import com.hypermind.jarvis.contract.MappingAsset
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ResultStatus
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
class GuardedOperationsTest {
    private val mapping: DeviceMapping =
        (MappingAsset.load(ApplicationProvider.getApplicationContext()) as MappingState.Valid).mapping
    private val now = Instant.parse("2026-09-26T12:00:10Z")
    private val device = "6f1c2d3e-4a5b-4c6d-8e7f-00000000d001"
    private val ran = mutableListOf<String>()
    private val tap =
        Primitive {
            envelope,
            _,
            ->
            ran += envelope.opId
            ResultEnvelope.ok(envelope.opId, JsonObject(emptyMap()))
        }

    private fun envelope(op: String = "op-1") =
        OperationEnvelope(
            opId = op,
            taskId = "t",
            deviceId = device,
            capability = "app.interact",
            operation = "tap",
            primitive = "accessibility.tap",
            packageName = "com.example.notes",
            arguments = JsonObject(mapOf("view_id" to JsonPrimitive("send"))),
            mappingVersion = mapping.version,
            issuedAt = "2026-09-26T12:00:00Z",
            expiresAt = "2026-09-26T12:00:30Z",
        )

    private fun handler(
        grid: GridState,
        primitives: Map<String, Primitive> = mapOf("accessibility.tap" to tap),
        available: (PlatformDependency) -> Boolean = { true },
    ) = GuardedOperations(
        guard = { DeviceGuard(device, mapping) { now } },
        localState = { DeviceLocalState(grid, AppPolicy(nonSensitive = setOf("com.example.notes"))) },
        primitives = primitives,
        available = available,
    )

    private val on = GridState(mapOf("com.example.notes" to setOf(GridToggle.UI_INTERACTION)))

    @Test
    fun `a toggled-off app refuses even though the server sent it`() =
        runBlocking {
            val result = handler(GridState()).handle(envelope())
            assertEquals(ResultStatus.REFUSED, result.status)
            assertEquals(RefusalReason.TOGGLE_OFF, result.refusalReason)
            assertTrue(ran.isEmpty())
        }

    @Test
    fun `an allowed operation runs exactly its mapped primitive`() =
        runBlocking {
            val result = handler(on).handle(envelope())
            assertEquals(ResultStatus.OK, result.status)
            assertEquals(listOf("op-1"), ran)
        }

    @Test
    fun `a primitive this build lacks fails explicitly`() =
        runBlocking {
            val result = handler(on, primitives = emptyMap()).handle(envelope())
            assertEquals(ResultStatus.FAILED, result.status)
            assertEquals(FailureReason.ACTION_FAILED, result.failureReason)
        }

    @Test
    fun `a device that is not enrolled fails without executing`() =
        runBlocking {
            val result =
                GuardedOperations(
                    guard = { null },
                    localState = { DeviceLocalState(on, null) },
                    primitives = mapOf("accessibility.tap" to tap),
                    available = { true },
                ).handle(envelope())
            assertEquals(ResultStatus.FAILED, result.status)
            assertTrue(ran.isEmpty())
        }

    // ── on-demand dependencies (docs/23 §5.3) ───────────────────────────

    private fun forceStop(op: String = "op-fs") =
        envelope(
            op,
        ).copy(operation = "force_stop", primitive = "shizuku.force_stop_package", arguments = JsonObject(emptyMap()))

    @Test
    fun `a missing dependency is an explicit refusal naming it and nothing runs`() =
        runBlocking {
            val stop =
                Primitive { e, _ ->
                    ran += e.opId
                    ResultEnvelope.ok(e.opId, JsonObject(emptyMap()))
                }
            val result =
                handler(on, primitives = mapOf("shizuku.force_stop_package" to stop)) {
                    it != PlatformDependency.SHIZUKU
                }.handle(forceStop())
            assertEquals(ResultStatus.REFUSED, result.status)
            assertEquals(RefusalReason.PLATFORM_UNAVAILABLE, result.refusalReason)
            assertEquals(PlatformDependency.SHIZUKU, result.requiredPlatform)
            assertTrue(ran.isEmpty())
        }

    @Test
    fun `availability is read at the moment each operation runs`() =
        runBlocking {
            var accessibility = true
            val handler = handler(on) { it != PlatformDependency.ACCESSIBILITY_SERVICE || accessibility }
            assertEquals(ResultStatus.OK, handler.handle(envelope("a")).status)
            accessibility = false // the user turned the service off
            val second = handler.handle(envelope("b"))
            assertEquals(RefusalReason.PLATFORM_UNAVAILABLE, second.refusalReason)
            assertEquals(PlatformDependency.ACCESSIBILITY_SERVICE, second.requiredPlatform)
            assertEquals(listOf("a"), ran)
        }

    // ── a result checks itself before it can leave ──────────────────────

    private fun readScreen(op: String = "op-rs") =
        envelope(op).copy(
            capability = "device.read",
            operation = "read_screen",
            primitive = "accessibility.read_tree",
            arguments = JsonObject(emptyMap()),
        )

    private val reading = GridState(mapOf("com.example.notes" to setOf(GridToggle.SCREEN_READ)))

    private fun answering(result: JsonObject, level: PerceptionLevel? = PerceptionLevel.ACCESSIBILITY) =
        mapOf("accessibility.read_tree" to Primitive { e, _ -> ResultEnvelope.ok(e.opId, result, level) })

    private fun screen(pkg: String, vararg nodes: Pair<String, JsonElement>) =
        buildJsonObject {
            putJsonObject("app") { put("package_name", pkg) }
            putJsonArray("nodes") {
                addJsonObject {
                    put("id", 0)
                    put("role", "android.widget.EditText")
                    putJsonArray("bounds") { repeat(4) { add(0) } }
                    nodes.forEach { (k, v) -> put(k, v) }
                }
            }
        }

    @Test
    fun `a well-formed screen read is sent`() =
        runBlocking {
            val result = handler(reading, answering(screen("com.example.notes"))).handle(readScreen())
            assertEquals(ResultStatus.OK, result.status)
        }

    @Test
    fun `an unredacted password node is never sent`() =
        runBlocking {
            val leaked =
                screen(
                    "com.example.notes",
                    "password" to JsonPrimitive(true),
                    "text" to JsonPrimitive("hunter2"),
                )
            val result = handler(reading, answering(leaked)).handle(readScreen())
            assertEquals(ResultStatus.FAILED, result.status)
            assertEquals(FailureReason.INTERNAL, result.failureReason)
            assertEquals(null, result.result)
        }

    @Test
    fun `a result about another app is never sent`() =
        runBlocking {
            val result = handler(reading, answering(screen("com.bank.app"))).handle(readScreen())
            assertEquals(ResultStatus.FAILED, result.status)
            assertEquals(null, result.result)
        }

    @Test
    fun `a result over the primitive's size bound is never sent`() =
        runBlocking {
            val big = screen("com.example.notes", "text" to JsonPrimitive("x".repeat(500)))
            val many =
                buildJsonObject {
                    put("app", big.getValue("app"))
                    putJsonArray("nodes") {
                        repeat(299) { i ->
                            addJsonObject {
                                put("id", i)
                                put("role", "r")
                                put("text", "x".repeat(500))
                                putJsonArray("bounds") { repeat(4) { add(0) } }
                            }
                        }
                    }
                }
            val result = handler(reading, answering(many)).handle(readScreen())
            assertEquals(ResultStatus.FAILED, result.status)
        }
}
