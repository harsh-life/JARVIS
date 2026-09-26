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
