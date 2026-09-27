package com.hypermind.jarvis.push

import com.hypermind.jarvis.channel.WakeResult
import com.hypermind.jarvis.push.WakeHandler.Outcome
import org.junit.Assert.assertEquals
import org.junit.Test

/** A push may only (re)connect the authenticated channel — idempotently (docs/23 §4). */
class WakeHandlerTest {
    private val wake = mapOf("type" to "wake")
    private var enrolled = true
    private var wanted = true
    private var channelResult = WakeResult.NOT_RUNNING
    private var channelCalls = 0
    private var starts = 0
    private var startAllowed = true
    private var now = 1_000_000L

    private val handler =
        WakeHandler(
            enrolled = { enrolled },
            connectionWanted = { wanted },
            channel = {
                channelCalls++
                channelResult
            },
            startChannelService = {
                if (startAllowed) starts++
                startAllowed
            },
            nowMillis = { now },
        )

    @Test
    fun `only the exact wake is acted on`() {
        for (data in listOf(
            emptyMap(),
            mapOf("type" to "sync"),
            mapOf("type" to "wake", "op_id" to "op-1"),
            mapOf("type" to "wake", "operation" to "tap", "package_name" to "com.bank"),
            mapOf("type" to "confirm", "confirmation_token" to "ct"),
        )) {
            assertEquals(Outcome.IGNORED_NOT_A_WAKE, handler.onWake(data))
        }
        assertEquals(0, channelCalls)
        assertEquals(0, starts)
    }

    @Test
    fun `a revoked or un-enrolled phone ignores wakes`() {
        enrolled = false
        assertEquals(Outcome.IGNORED_NOT_ENROLLED, handler.onWake(wake))
        assertEquals(0, channelCalls + starts)
    }

    @Test
    fun `a wake never overrides the user's Disconnect`() {
        wanted = false
        assertEquals(Outcome.IGNORED_DISCONNECTED_BY_USER, handler.onWake(wake))
        assertEquals(0, channelCalls + starts)
    }

    @Test
    fun `a wake while connected or connecting does nothing`() {
        channelResult = WakeResult.ALREADY_CONNECTED
        repeat(3) { assertEquals(Outcome.ALREADY_CONNECTED, handler.onWake(wake)) }
        assertEquals(0, starts)
    }

    @Test
    fun `a wake while backing off reconnects now`() {
        channelResult = WakeResult.RECONNECTING
        assertEquals(Outcome.RECONNECTING_NOW, handler.onWake(wake))
        assertEquals(0, starts)
    }

    @Test
    fun `a stopped channel is started once per burst of duplicate wakes`() {
        repeat(5) { handler.onWake(wake) }
        assertEquals(1, starts)
        now += 60_000 // a later outage: start again
        assertEquals(Outcome.STARTED, handler.onWake(wake))
        assertEquals(2, starts)
    }

    @Test
    fun `an app needing an update is not restarted by a wake`() {
        channelResult = WakeResult.BLOCKED
        assertEquals(Outcome.IGNORED_UPDATE_REQUIRED, handler.onWake(wake))
        assertEquals(0, starts)
    }

    @Test
    fun `a refused background start is reported, and retried by the next wake`() {
        startAllowed = false
        assertEquals(Outcome.START_REFUSED, handler.onWake(wake))
        startAllowed = true
        assertEquals(Outcome.STARTED, handler.onWake(wake))
    }
}
