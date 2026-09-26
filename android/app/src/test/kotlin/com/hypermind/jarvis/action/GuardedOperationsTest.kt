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
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ResultStatus
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
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
    ) = GuardedOperations(
        guard = { DeviceGuard(device, mapping) { now } },
        localState = { DeviceLocalState(grid, AppPolicy(nonSensitive = setOf("com.example.notes"))) },
        primitives = primitives,
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
                ).handle(envelope())
            assertEquals(ResultStatus.FAILED, result.status)
            assertTrue(ran.isEmpty())
        }
}
