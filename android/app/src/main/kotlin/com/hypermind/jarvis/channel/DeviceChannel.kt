package com.hypermind.jarvis.channel

import com.hypermind.jarvis.auth.EnrollmentLost
import com.hypermind.jarvis.contract.CloseCode
import com.hypermind.jarvis.contract.Hello
import com.hypermind.jarvis.contract.Reauth
import com.hypermind.jarvis.contract.ServerFrame
import com.hypermind.jarvis.contract.encode
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.io.IOException
import java.time.Instant
import java.time.OffsetDateTime
import kotlin.random.Random

/** What the channel needs from the auth layer. */
interface ChannelCredentials {
    /** A current access token and its expiry; [EnrollmentLost] if the device can no longer authenticate. */
    fun accessToken(forceRefresh: Boolean): Pair<String, Instant>

    /** A fresh single-use device proof. */
    fun freshProof(): String
}

/**
 * The device side of the authenticated channel (docs/23 §4).
 *
 * * Authenticates on every connect with a current access token **and** a fresh
 *   device proof in the first frame; re-authenticates before the token
 *   expires.
 * * Runs each operation in its own coroutine; `cancel` (by op or by task)
 *   cancels it and its result is discarded, never sent late.
 * * When the socket dies, every in-flight operation is cancelled — authority
 *   does not outlive the connection that carried it — and the channel
 *   reconnects with jittered exponential backoff, or at once when the network
 *   returns.
 * * `revoked` wipes the enrollment ([onRevoked]) and stops for good; a mapping
 *   mismatch stops until the app is updated. Neither retries.
 */
class DeviceChannel(
    private val http: OkHttpClient,
    private val url: () -> String?,
    private val credentials: ChannelCredentials,
    private val mappingVersion: String,
    private val clientVersion: String,
    private val handler: OperationHandler,
    private val scope: CoroutineScope,
    private val onRevoked: () -> Unit,
    private val now: () -> Instant = Instant::now,
    private val random: Random = Random.Default,
) {
    private val _state = MutableStateFlow<ChannelState>(ChannelState.Stopped)
    val state: StateFlow<ChannelState> = _state.asStateFlow()

    private var socket: WebSocket? = null
    private var running = false
    private var attempt = 0
    private var reconnectJob: Job? = null
    private var reauthJob: Job? = null
    private val inFlight = mutableMapOf<String, Pair<String, Job>>() // opId → (taskId, job)

    @Synchronized
    fun start() {
        if (running) return
        running = true
        attempt = 0
        connect()
    }

    @Synchronized
    fun stop() {
        running = false
        reconnectJob?.cancel()
        reauthJob?.cancel()
        cancelAllOperations()
        socket?.close(NORMAL_CLOSURE, "stopped")
        socket = null
        _state.value = ChannelState.Stopped
    }

    /** The network came back: retry now instead of waiting out the backoff. */
    @Synchronized
    fun networkAvailable() {
        if (running && socket == null) {
            reconnectJob?.cancel()
            connect()
        }
    }

    private fun connect() {
        val endpoint = url() ?: return fail("no server configured")
        _state.value = ChannelState.Connecting
        val request = Request.Builder().url(endpoint).build()
        socket = http.newWebSocket(request, Listener())
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(
            webSocket: WebSocket,
            response: Response,
        ) {
            scope.launch {
                val hello =
                    try {
                        val (token, _) = credentials.accessToken(forceRefresh = false)
                        Hello(
                            accessToken = token,
                            deviceProof = credentials.freshProof(),
                            mappingVersion = mappingVersion,
                            clientVersion = clientVersion,
                        )
                    } catch (ignored: EnrollmentLost) {
                        webSocket.close(NORMAL_CLOSURE, "enrollment lost")
                        revoked()
                        return@launch
                    } catch (ignored: IOException) {
                        webSocket.close(NORMAL_CLOSURE, "token refresh failed")
                        return@launch
                    }
                webSocket.send(hello.encode())
            }
        }

        override fun onMessage(
            webSocket: WebSocket,
            text: String,
        ) {
            if (webSocket !== socket) return
            when (val frame = ServerFrame.parse(text)) {
                is ServerFrame.HelloOk -> connected(OffsetDateTime.parse(frame.ack.sessionExpiresAt).toInstant())
                is ServerFrame.ReauthOk -> connected(OffsetDateTime.parse(frame.ack.sessionExpiresAt).toInstant())
                is ServerFrame.Operation -> run(webSocket, frame)
                is ServerFrame.Cancel -> cancel(frame.cancel.opId, frame.cancel.taskId)
                is ServerFrame.Invalid -> webSocket.close(CloseCode.PROTOCOL_ERROR.code, "invalid frame")
            }
        }

        override fun onClosing(
            webSocket: WebSocket,
            code: Int,
            reason: String,
        ) {
            webSocket.close(code, null)
            closed(webSocket, code, reason)
        }

        override fun onClosed(
            webSocket: WebSocket,
            code: Int,
            reason: String,
        ) = closed(webSocket, code, reason)

        override fun onFailure(
            webSocket: WebSocket,
            t: Throwable,
            response: Response?,
        ) = closed(webSocket, null, "")
    }

    @Synchronized
    private fun connected(expiresAt: Instant) {
        attempt = 0
        _state.value = ChannelState.Connected(expiresAt)
        reauthJob?.cancel()
        reauthJob =
            scope.launch {
                val wait = expiresAt.toEpochMilli() - now().toEpochMilli() - REAUTH_MARGIN_MILLIS
                delay(wait.coerceAtLeast(0))
                reauth()
            }
    }

    private fun reauth() {
        val current = socket ?: return
        try {
            val (token, _) = credentials.accessToken(forceRefresh = true)
            current.send(Reauth(accessToken = token).encode())
        } catch (ignored: EnrollmentLost) {
            current.close(NORMAL_CLOSURE, "enrollment lost")
            revoked()
        } catch (ignored: IOException) {
            // The server closes at expiry; the reconnect path authenticates anew.
        }
    }

    @Synchronized
    private fun run(
        webSocket: WebSocket,
        frame: ServerFrame.Operation,
    ) {
        val envelope = frame.envelope
        if (inFlight.containsKey(envelope.opId)) return
        // Registered before it can run, so even an immediately-completing
        // handler finds its own entry.
        val job =
            scope.launch(start = CoroutineStart.LAZY) {
                val result = handler.handle(envelope)
                synchronized(this@DeviceChannel) {
                    // Only a still-current operation on the still-current socket
                    // answers; a cancelled or orphaned one is discarded.
                    if (inFlight.remove(envelope.opId) != null && webSocket === socket) {
                        webSocket.send(result.encode())
                    }
                }
            }
        inFlight[envelope.opId] = envelope.taskId to job
        job.start()
    }

    @Synchronized
    private fun cancel(
        opId: String?,
        taskId: String?,
    ) {
        val targets =
            inFlight.filter { (id, entry) -> (opId != null && id == opId) || (taskId != null && entry.first == taskId) }
        targets.forEach { (id, entry) ->
            inFlight.remove(id)
            entry.second.cancel()
        }
    }

    private fun cancelAllOperations() {
        inFlight.values.forEach { it.second.cancel() }
        inFlight.clear()
    }

    @Synchronized
    private fun closed(
        webSocket: WebSocket,
        code: Int?,
        reason: String,
    ) {
        if (webSocket !== socket) return
        socket = null
        reauthJob?.cancel()
        cancelAllOperations()
        if (!running) return
        when (CloseCode.of(code ?: 0)) {
            CloseCode.REVOKED -> revoked()
            CloseCode.MAPPING_VERSION_MISMATCH -> {
                running = false
                _state.value = ChannelState.UpdateRequired(serverVersion = reason)
            }
            CloseCode.CHANNEL_DISABLED -> {
                _state.value = ChannelState.Disabled
                scheduleReconnect(DISABLED_RETRY_MILLIS, "channel disabled")
            }
            CloseCode.AUTH_EXPIRED, CloseCode.AUTH_FAILED -> {
                refreshThenReconnect()
            }
            CloseCode.SUPERSEDED, CloseCode.PROTOCOL_ERROR, null -> backoff("connection lost")
        }
    }

    private fun refreshThenReconnect() {
        scope.launch {
            try {
                credentials.accessToken(forceRefresh = true)
                synchronized(this@DeviceChannel) { backoff("re-authenticating") }
            } catch (ignored: EnrollmentLost) {
                revoked()
            } catch (ignored: IOException) {
                synchronized(this@DeviceChannel) { backoff("server unreachable") }
            }
        }
    }

    private fun backoff(reason: String) {
        attempt += 1
        val base = (BASE_DELAY_MILLIS shl (attempt - 1).coerceAtMost(MAX_SHIFT)).coerceAtMost(MAX_DELAY_MILLIS)
        val jitter = (base * JITTER * (random.nextDouble() * 2 - 1)).toLong()
        scheduleReconnect(base + jitter, reason)
    }

    private fun scheduleReconnect(
        delayMillis: Long,
        reason: String,
    ) {
        if (_state.value !is ChannelState.Disabled) {
            _state.value = ChannelState.Reconnecting(attempt, delayMillis, reason)
        }
        reconnectJob?.cancel()
        reconnectJob =
            scope.launch {
                delay(delayMillis)
                synchronized(this@DeviceChannel) { if (running && socket == null) connect() }
            }
    }

    @Synchronized
    private fun revoked() {
        running = false
        reconnectJob?.cancel()
        reauthJob?.cancel()
        cancelAllOperations()
        socket = null
        _state.value = ChannelState.Revoked
        onRevoked()
    }

    private fun fail(reason: String) {
        backoff(reason)
    }

    private companion object {
        const val NORMAL_CLOSURE = 1000
        const val REAUTH_MARGIN_MILLIS = 60_000L
        const val BASE_DELAY_MILLIS = 1_000L
        const val MAX_DELAY_MILLIS = 60_000L
        const val MAX_SHIFT = 6
        const val JITTER = 0.2
        const val DISABLED_RETRY_MILLIS = 5 * 60_000L
    }
}
