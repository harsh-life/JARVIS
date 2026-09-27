package com.hypermind.jarvis.push

import com.hypermind.jarvis.contract.FcmClientOptions

/**
 * The push SDK as the rest of the app sees it (docs/23 §4). One real
 * implementation ([FirebasePushClient]); tests use a fake. Nothing here can
 * run an operation: the only thing that ever comes back is a registration
 * token, and the only thing a push can cause is a reconnect ([WakeHandler]).
 */
interface PushClient {
    /**
     * Start receiving wakes with these (public) identifiers. The current
     * token — and every later rotation — arrives through [onToken]; a device
     * without Google Play services, or any SDK failure, reports through
     * [onUnavailable]. Never throws.
     */
    fun enable(
        options: FcmClientOptions,
        onToken: (String) -> Unit,
        onUnavailable: (String) -> Unit,
    )

    /** Stop: delete the token, stop auto-registration, disable the receiver. Never throws. */
    fun disable()
}
