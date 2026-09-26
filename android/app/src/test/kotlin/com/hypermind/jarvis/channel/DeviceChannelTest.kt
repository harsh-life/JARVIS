package com.hypermind.jarvis.channel

import com.hypermind.jarvis.auth.EnrollmentLost
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.ResultEnvelope
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancel
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.random.Random

/**
 * The device side of docs/23 §4 against a real WebSocket peer (MockWebServer
 * plays the server). Pinned: authentication on every connect, results only
 * for live operations, cancellation discards, and each close code's recovery
 * — including the ones that must never retry.
 */
class DeviceChannelTest {
    private val server = MockWebServer().apply { start() }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val connections = LinkedBlockingQueue<ServerSide>()
    private val allSides = mutableListOf<ServerSide>()
    private val refreshes = AtomicInteger()
    private var revoked = 0
    private var enrollmentLost = false

    @After
    fun tearDown() {
        scope.cancel()
        // Server-side sockets must be gone before MockWebServer can shut down.
        allSides.forEach { side -> if (side.opened) side.socket.close(1000, null) }
        // Every assertion has run; a socket still draining is not a test result.
        try {
            server.shutdown()
        } catch (ignored: java.io.IOException) {
            // MockWebServer: "Gave up waiting for queue to shut down".
        }
    }

    /** One accepted socket, from the server's point of view. */
    class ServerSide : WebSocketListener() {
        lateinit var socket: WebSocket
        val opened: Boolean get() = this::socket.isInitialized
        val received = LinkedBlockingQueue<JsonObject>()
        val closed = LinkedBlockingQueue<Int>()

        override fun onOpen(
            webSocket: WebSocket,
            response: Response,
        ) {
            socket = webSocket
        }

        override fun onMessage(
            webSocket: WebSocket,
            text: String,
        ) {
            received += Json.parseToJsonElement(text).jsonObject
        }

        override fun onClosing(
            webSocket: WebSocket,
            code: Int,
            reason: String,
        ) {
            closed += code
        }

        fun next(): JsonObject = requireNotNull(received.poll(5, TimeUnit.SECONDS)) { "no frame from the device" }
    }

    private fun acceptConnections(count: Int) =
        repeat(count) {
            val side = ServerSide()
            connections += side
            allSides += side
            server.enqueue(MockResponse().withWebSocketUpgrade(side))
        }

    private fun channel(handler: OperationHandler): DeviceChannel =
        DeviceChannel(
            http = OkHttpClient(),
            url = { server.url("/api/v1/devices/channel").toString().replaceFirst("http", "ws") },
            credentials =
                object : ChannelCredentials {
                    override fun accessToken(forceRefresh: Boolean): Pair<String, Instant> {
                        if (enrollmentLost) throw EnrollmentLost()
                        if (forceRefresh) refreshes.incrementAndGet()
                        return "token-${refreshes.get()}" to Instant.now().plusSeconds(900)
                    }

                    override fun freshProof(): String = "v1.proof.${System.nanoTime()}"
                },
            mappingVersion = "1-0123456789abcdef",
            clientVersion = "test",
            handler = handler,
            scope = scope,
            onRevoked = { revoked += 1 },
            random = Random(0),
        )

    private fun connected(
        channel: DeviceChannel,
        side: ServerSide,
    ): JsonObject {
        val hello = side.next()
        side.socket.send(
            """{"type":"hello_ok","device_id":"d","mapping_version":"1-0123456789abcdef",""" +
                """"server_time":"2026-09-26T12:00:00Z","session_expires_at":"2099-01-01T00:00:00Z"}""",
        )
        waitFor { channel.state.value is ChannelState.Connected }
        return hello
    }

    private fun operation(
        opId: String = "op-1",
        taskId: String = "task-1",
    ) = """{"type":"operation","op_id":"$opId","task_id":"$taskId","device_id":"d","capability":"device.read",""" +
        """"operation":"read_battery","primitive":"android.api.battery_state","arguments":{},""" +
        """"mapping_version":"1-0123456789abcdef",""" +
        """"issued_at":"2026-09-26T12:00:00Z","expires_at":"2026-09-26T12:00:30Z"}"""

    private fun waitFor(
        timeoutMillis: Long = 5_000,
        condition: () -> Boolean,
    ) {
        val deadline = System.currentTimeMillis() + timeoutMillis
        while (!condition()) {
            check(System.currentTimeMillis() < deadline) { "condition not met in time" }
            Thread.sleep(10)
        }
    }

    @Test
    fun `every connect authenticates with a token and a fresh proof in the first frame`() {
        acceptConnections(1)
        val channel = channel(UnimplementedOperations).also { it.start() }
        val side = connections.take()
        val hello = connected(channel, side)
        assertEquals("hello", hello.getValue("type").jsonPrimitive.content)
        assertEquals(
            setOf("type", "access_token", "device_proof", "mapping_version", "client_version"),
            hello.keys,
        )
        assertEquals("1-0123456789abcdef", hello.getValue("mapping_version").jsonPrimitive.content)
        // Nothing about the user, graph, or capabilities is ever asserted by the device.
        assertTrue(hello.keys.none { it in setOf("user_id", "graph_id", "capabilities") })
        assertTrue(
            server
                .takeRequest()
                .requestUrl
                .toString()
                .let { "token" !in it && "proof" !in it },
        )
        channel.stop()
    }

    @Test
    fun `platform status is reported only on an authenticated connection`() {
        acceptConnections(1)
        val channel = channel(UnimplementedOperations)
        // Not connected: nothing to send it on, and nothing is queued for later.
        channel.reportPlatforms(mapOf(PlatformDependency.SHIZUKU to false))
        channel.start()
        val side = connections.take()
        connected(channel, side)
        channel.reportPlatforms(
            mapOf(
                PlatformDependency.ACCESSIBILITY_SERVICE to true,
                PlatformDependency.SHIZUKU to false,
            ),
        )
        val status = side.next()
        assertEquals("platform_status", status.getValue("type").jsonPrimitive.content)
        assertEquals(
            mapOf("accessibility_service" to "true", "shizuku" to "false"),
            status.getValue("platforms").jsonObject.mapValues { it.value.jsonPrimitive.content },
        )
        assertNull(side.received.poll(200, TimeUnit.MILLISECONDS))
        channel.stop()
    }

    @Test
    fun `an operation is answered once with its own op id`() {
        acceptConnections(1)
        val channel =
            channel { envelope: OperationEnvelope ->
                ResultEnvelope.ok(envelope.opId, JsonObject(emptyMap()))
            }.also { it.start() }
        val side = connections.take()
        connected(channel, side)
        side.socket.send(operation())
        val result = side.next()
        assertEquals("result", result.getValue("type").jsonPrimitive.content)
        assertEquals("op-1", result.getValue("op_id").jsonPrimitive.content)
        assertEquals("ok", result.getValue("status").jsonPrimitive.content)
        channel.stop()
    }

    @Test
    fun `a cancelled operation is aborted and its result is never sent`() {
        acceptConnections(1)
        val started = CompletableDeferred<Unit>()
        val aborted = CompletableDeferred<Unit>()
        val channel =
            channel { _ ->
                started.complete(Unit)
                try {
                    awaitCancellation()
                } finally {
                    aborted.complete(Unit)
                }
            }.also { it.start() }
        val side = connections.take()
        connected(channel, side)
        side.socket.send(operation(opId = "op-9", taskId = "task-9"))
        waitFor { started.isCompleted }
        side.socket.send("""{"type":"cancel","task_id":"task-9"}""")
        waitFor { aborted.isCompleted }
        assertNull(side.received.poll(300, TimeUnit.MILLISECONDS))
        channel.stop()
    }

    @Test
    fun `losing the socket aborts in-flight work and reconnects`() {
        acceptConnections(2)
        val started = CompletableDeferred<Unit>()
        val aborted = CompletableDeferred<Unit>()
        val channel =
            channel { _ ->
                started.complete(Unit)
                try {
                    awaitCancellation()
                } finally {
                    aborted.complete(Unit)
                }
            }.also { it.start() }
        val first = connections.take()
        connected(channel, first)
        first.socket.send(operation())
        waitFor { started.isCompleted }
        first.socket.close(1001, "going away")
        waitFor { aborted.isCompleted }
        val second = connections.take()
        connected(channel, second)
        channel.stop()
    }

    @Test
    fun `revoked wipes the enrollment and never reconnects`() {
        acceptConnections(2)
        val channel = channel(UnimplementedOperations).also { it.start() }
        val side = connections.take()
        connected(channel, side)
        side.socket.close(4003, "device revoked")
        waitFor { channel.state.value is ChannelState.Revoked }
        assertEquals(1, revoked)
        Thread.sleep(1_500)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `a mapping mismatch stops until the app is updated`() {
        acceptConnections(2)
        val channel = channel(UnimplementedOperations).also { it.start() }
        val side = connections.take()
        side.next()
        side.socket.close(4004, "1-ffffffffffffffff")
        waitFor { channel.state.value is ChannelState.UpdateRequired }
        assertEquals("1-ffffffffffffffff", (channel.state.value as ChannelState.UpdateRequired).serverVersion)
        Thread.sleep(1_500)
        assertEquals(1, server.requestCount)
        assertEquals(0, revoked)
    }

    @Test
    fun `an expired session re-authenticates and reconnects`() {
        acceptConnections(2)
        val channel = channel(UnimplementedOperations).also { it.start() }
        val first = connections.take()
        connected(channel, first)
        first.socket.close(4002, "auth_expired")
        val second = connections.take()
        val hello = connected(channel, second)
        assertTrue(refreshes.get() >= 1)
        assertEquals("token-${refreshes.get()}", hello.getValue("access_token").jsonPrimitive.content)
        channel.stop()
    }

    @Test
    fun `a device that can no longer authenticate is treated as revoked`() {
        acceptConnections(1)
        enrollmentLost = true
        val channel = channel(UnimplementedOperations).also { it.start() }
        waitFor { channel.state.value is ChannelState.Revoked }
        assertEquals(1, revoked)
    }

    @Test
    fun `an invalid frame from the server is a protocol error, not guessed at`() {
        acceptConnections(2)
        val channel = channel(UnimplementedOperations).also { it.start() }
        val first = connections.take()
        connected(channel, first)
        first.socket.send("""{"type":"operation","op_id":"x","authorized":true}""")
        assertEquals(4008, first.closed.poll(5, TimeUnit.SECONDS))
        channel.stop()
    }
}
