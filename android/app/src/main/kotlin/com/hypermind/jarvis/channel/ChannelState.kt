package com.hypermind.jarvis.channel

import java.time.Instant

/** What the device channel is doing, for the UI and the presentation layer. */
sealed interface ChannelState {
    data object Stopped : ChannelState

    data object Connecting : ChannelState

    data class Connected(
        val sessionExpiresAt: Instant,
    ) : ChannelState

    data class Reconnecting(
        val attempt: Int,
        val delayMillis: Long,
        val reason: String,
        val cause: ReconnectCause = ReconnectCause.CONNECTION_LOST,
    ) : ChannelState

    /** The server revoked this device, or its credential stopped working. */
    data object Revoked : ChannelState

    /** This app's mapping table differs from the server's: update the app. */
    data class UpdateRequired(
        val serverVersion: String,
    ) : ChannelState

    /** The operator has not enabled the device channel on the server. */
    data object Disabled : ChannelState
}

/** What a push wake did to the channel (docs/23 §4). */
enum class WakeResult {
    /** A socket exists (connected or connecting): nothing to do — never a second one. */
    ALREADY_CONNECTED,

    /** Running but between attempts: reconnecting now instead of waiting out the backoff. */
    RECONNECTING,

    /** Stopped: the caller decides whether to start the service. */
    NOT_RUNNING,

    /** Stopped until the app is updated (mapping mismatch): a wake cannot help. */
    BLOCKED,
}

/** Why the channel is reconnecting — typed, so the UI never parses [ChannelState.Reconnecting.reason]. */
enum class ReconnectCause {
    CONNECTION_LOST,
    AUTHENTICATION_EXPIRED,
    SERVER_UNREACHABLE,
    NOT_CONFIGURED,
}

/** One operation running on this phone: its kind, never its arguments or result. */
data class RunningOperation(
    val taskId: String,
    val capability: String,
    val operation: String,
    val primitive: String,
)
