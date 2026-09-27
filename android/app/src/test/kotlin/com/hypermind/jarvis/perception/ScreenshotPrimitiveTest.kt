package com.hypermind.jarvis.perception

import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.action.GuardedOperations
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.DeviceGuard
import com.hypermind.jarvis.contract.DeviceLocalState
import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.GridState
import com.hypermind.jarvis.contract.GridToggle
import com.hypermind.jarvis.contract.MappingAsset
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PerceptionCheck
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultStatus
import com.hypermind.jarvis.contract.ScreenshotResult
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.time.Instant
import java.util.Base64

/** `capture_screenshot` (docs/23 §6 level 4, ANDC-T7) against fake screens. */
@RunWith(RobolectricTestRunner::class)
class ScreenshotPrimitiveTest {
    private val mapping: DeviceMapping =
        (MappingAsset.load(ApplicationProvider.getApplicationContext()) as MappingState.Valid).mapping
    private val spec = mapping.lookup("device.read", "capture_screenshot")!!
    private val notes = "com.example.notes"
    private val device = "6f1c2d3e-4a5b-4c6d-8e7f-00000000d001"
    private val pixels = byteArrayOf(0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4)

    private fun envelope(pkg: String = notes) =
        OperationEnvelope(
            opId = "op",
            taskId = "t",
            deviceId = device,
            capability = "device.read",
            operation = "capture_screenshot",
            primitive = "accessibility.screenshot",
            packageName = pkg,
            arguments = JsonObject(emptyMap()),
            mappingVersion = mapping.version,
            issuedAt = "2026-09-26T12:00:00Z",
            expiresAt = "2026-09-26T12:00:30Z",
        )

    private class CountingCapture(
        private val outcome: () -> CaptureOutcome,
        private val during: () -> Unit = {},
    ) : ScreenCapture {
        var calls = 0

        override suspend fun capture(maxBytes: Int): CaptureOutcome {
            calls++
            during()
            return outcome()
        }
    }

    private fun window(pkg: String = notes) = ForegroundWindow(pkg, "$pkg.Main", "Notes", null)

    @Test
    fun `a capture of the named app is sent once, as a valid result, and its buffer is cleared`() =
        runBlocking {
            val frame = CaptureOutcome.Frame(pixels.copyOf(), 2, 2)
            val capture = CountingCapture({ frame })
            val out = ScreenshotPrimitive(FakeScreen(window()), capture).run(envelope(), spec)
            assertEquals(ResultStatus.OK, out.status)
            assertNull(PerceptionCheck.problem(spec.result, out.result!!, out.perceptionLevel, notes))
            val shot = ContractJson.decodeFromJsonElement(ScreenshotResult.serializer(), out.result!!)
            assertArrayEquals(pixels, Base64.getDecoder().decode(shot.imageWebpBase64))
            assertTrue(frame.webp.all { it == 0.toByte() })
            assertEquals(1, capture.calls)
        }

    @Test
    fun `a secure window is refused once and never retried`() =
        runBlocking {
            val capture = CountingCapture({ CaptureOutcome.SecureWindow })
            val out = ScreenshotPrimitive(FakeScreen(window()), capture).run(envelope(), spec)
            assertEquals(RefusalReason.SECURE_WINDOW, out.refusalReason)
            assertNull(out.result)
            assertEquals(1, capture.calls)
        }

    @Test
    fun `another app in front is refused before anything is captured`() =
        runBlocking {
            val capture = CountingCapture({ CaptureOutcome.Frame(pixels.copyOf(), 2, 2) })
            val out = ScreenshotPrimitive(FakeScreen(window("com.bank.app")), capture).run(envelope(), spec)
            assertEquals(RefusalReason.PACKAGE_MISMATCH, out.refusalReason)
            assertEquals(0, capture.calls)
        }

    @Test
    fun `an app switch during the capture discards the frame`() =
        runBlocking {
            val screen = FakeScreen(window())
            val frame = CaptureOutcome.Frame(pixels.copyOf(), 2, 2)
            val out =
                ScreenshotPrimitive(screen, CountingCapture({ frame }) { screen.window = window("com.bank.app") })
                    .run(envelope(), spec)
            assertEquals(RefusalReason.PACKAGE_MISMATCH, out.refusalReason)
            assertNull(out.result)
            assertTrue(frame.webp.all { it == 0.toByte() })
        }

    @Test
    fun `a failed capture is an explicit failure`() =
        runBlocking {
            val out =
                ScreenshotPrimitive(
                    FakeScreen(window()),
                    CountingCapture({
                        CaptureOutcome.Unavailable
                    }),
                ).run(envelope(), spec)
            assertEquals(FailureReason.ACTION_FAILED, out.failureReason)
        }

    // ── the guard decides before the primitive is reached ───────────────

    private fun guarded(
        grid: GridState,
        policy: AppPolicy,
        capture: ScreenCapture,
    ) = GuardedOperations(
        guard = { DeviceGuard(device, mapping) { Instant.parse("2026-09-26T12:00:10Z") } },
        localState = { DeviceLocalState(grid, policy) },
        primitives = mapOf("accessibility.screenshot" to ScreenshotPrimitive(FakeScreen(window()), capture)),
        available = { true },
    )

    @Test
    fun `the screenshot toggle is off by default and nothing is captured`() =
        runBlocking {
            val capture = CountingCapture({ CaptureOutcome.Frame(pixels.copyOf(), 2, 2) })
            val grid = GridState(mapOf(notes to setOf(GridToggle.SCREEN_READ, GridToggle.UI_INTERACTION)))
            val out = guarded(grid, AppPolicy(nonSensitive = setOf(notes)), capture).handle(envelope())
            assertEquals(RefusalReason.TOGGLE_OFF, out.refusalReason)
            assertEquals(0, capture.calls)
        }

    @Test
    fun `a sensitive app is never captured even with its toggle on`() =
        runBlocking {
            val capture = CountingCapture({ CaptureOutcome.Frame(pixels.copyOf(), 2, 2) })
            val grid = GridState(mapOf(notes to setOf(GridToggle.SCREENSHOT)))
            val out = guarded(grid, AppPolicy(sensitive = setOf(notes)), capture).handle(envelope())
            assertEquals(RefusalReason.SENSITIVE_PACKAGE, out.refusalReason)
            assertEquals(0, capture.calls)
        }
}
