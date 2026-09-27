package com.hypermind.jarvis.privileged

import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultStatus
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** `shizuku.force_stop_package` (docs/23 §5.3) — typed, on demand, never a shell. */
class ForceStopPrimitiveTest {
    private val mapping =
        DeviceMapping.load(
            File(requireNotNull(System.getProperty("jarvis.shared.dir"))).resolve("device_mapping.json").readText(),
        )
    private val spec = mapping.lookup("app.interact", "force_stop")!!

    private class FakeShizuku(
        var outcome: PrivilegedOutcome,
    ) : ShizukuGateway {
        val stopped = mutableListOf<String>()

        override fun available() = outcome != PrivilegedOutcome.UNAVAILABLE

        override suspend fun forceStop(packageName: String): PrivilegedOutcome {
            stopped += packageName
            return outcome
        }
    }

    private fun envelope(pkg: String = "com.example.notes") =
        OperationEnvelope(
            opId = "op",
            taskId = "t",
            deviceId = "d",
            capability = "app.interact",
            operation = "force_stop",
            primitive = spec.primitive,
            packageName = pkg,
            arguments = JsonObject(emptyMap()),
            mappingVersion = mapping.version,
            issuedAt = "2026-09-26T12:00:00Z",
            expiresAt = "2026-09-26T12:00:30Z",
        )

    @Test
    fun `the mapped primitive is typed and takes no arguments`() {
        assertEquals("shizuku.force_stop_package", spec.primitive)
        assertTrue(spec.arguments.isEmpty())
        assertEquals(listOf(PlatformDependency.SHIZUKU), spec.dependencies)
    }

    @Test
    fun `it stops exactly the envelope's package`() =
        runBlocking {
            val shizuku = FakeShizuku(PrivilegedOutcome.DONE)
            val out = ForceStopPrimitive(shizuku, "com.hypermind.jarvis").run(envelope(), spec)
            assertEquals(ResultStatus.OK, out.status)
            assertEquals(listOf("com.example.notes"), shizuku.stopped)
        }

    @Test
    fun `a binding lost mid-call is an explicit platform refusal naming Shizuku`() =
        runBlocking {
            val out =
                ForceStopPrimitive(
                    FakeShizuku(PrivilegedOutcome.UNAVAILABLE),
                    "com.hypermind.jarvis",
                ).run(envelope(), spec)
            assertEquals(RefusalReason.PLATFORM_UNAVAILABLE, out.refusalReason)
            assertEquals(PlatformDependency.SHIZUKU, out.requiredPlatform)
            assertNull(out.result)
        }

    @Test
    fun `a failed stop is a failure, not a success`() =
        runBlocking {
            val out =
                ForceStopPrimitive(
                    FakeShizuku(PrivilegedOutcome.FAILED),
                    "com.hypermind.jarvis",
                ).run(envelope(), spec)
            assertEquals(FailureReason.ACTION_FAILED, out.failureReason)
        }

    @Test
    fun `it never stops this app itself`() =
        runBlocking {
            val shizuku = FakeShizuku(PrivilegedOutcome.DONE)
            val out = ForceStopPrimitive(shizuku, "com.hypermind.jarvis").run(envelope("com.hypermind.jarvis"), spec)
            assertEquals(ResultStatus.FAILED, out.status)
            assertTrue(shizuku.stopped.isEmpty())
        }
}
