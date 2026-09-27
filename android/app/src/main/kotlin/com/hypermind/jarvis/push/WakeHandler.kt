package com.hypermind.jarvis.push

import com.hypermind.jarvis.channel.WakeResult
import com.hypermind.jarvis.contract.WakeSignal

/**
 * What a received push may do (docs/23 §4): make sure the authenticated
 * channel is up — and nothing else.
 *
 * * Only a data map exactly equal to the wake is acted on (ANDC-T9).
 * * Nothing is executed, approved or read from the push: operations still
 *   arrive only over the channel, after it authenticated with a current access
 *   token and a fresh device proof, and each still passes the device guard.
 * * Idempotent: a wake while connected or connecting does nothing (never a
 *   second socket); while backing off it reconnects now; while stopped it
 *   starts the channel's foreground service — but only if the user left
 *   JARVIS connected. A wake never overrides Disconnect, and a revoked or
 *   un-enrolled phone ignores wakes entirely.
 */
class WakeHandler(
    private val enrolled: () -> Boolean,
    private val connectionWanted: () -> Boolean,
    private val channel: () -> WakeResult,
    private val startChannelService: () -> Boolean,
    private val nowMillis: () -> Long = System::currentTimeMillis,
) {
    private var startedAt: Long? = null

    enum class Outcome {
        IGNORED_NOT_A_WAKE,
        IGNORED_NOT_ENROLLED,
        IGNORED_DISCONNECTED_BY_USER,
        IGNORED_UPDATE_REQUIRED,
        ALREADY_CONNECTED,
        RECONNECTING_NOW,
        STARTED,
        ALREADY_STARTING,
        START_REFUSED,
    }

    @Synchronized
    fun onWake(data: Map<String, String>): Outcome {
        if (!WakeSignal.isWake(data)) return Outcome.IGNORED_NOT_A_WAKE
        if (!enrolled()) return Outcome.IGNORED_NOT_ENROLLED
        if (!connectionWanted()) return Outcome.IGNORED_DISCONNECTED_BY_USER
        return when (channel()) {
            WakeResult.ALREADY_CONNECTED -> Outcome.ALREADY_CONNECTED
            WakeResult.RECONNECTING -> Outcome.RECONNECTING_NOW
            WakeResult.BLOCKED -> Outcome.IGNORED_UPDATE_REQUIRED
            WakeResult.NOT_RUNNING -> start()
        }
    }

    /** One service start per burst: duplicate wakes while it is starting do nothing. */
    private fun start(): Outcome {
        val now = nowMillis()
        startedAt?.let { if (now - it in 0 until START_DEBOUNCE_MILLIS) return Outcome.ALREADY_STARTING }
        if (!startChannelService()) return Outcome.START_REFUSED
        startedAt = now
        return Outcome.STARTED
    }

    private companion object {
        const val START_DEBOUNCE_MILLIS = 10_000L
    }
}
