package com.hypermind.jarvis.push

import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.EnrollmentLost
import com.hypermind.jarvis.contract.PushClientConfig
import com.hypermind.jarvis.contract.PushProviderKind
import com.hypermind.jarvis.contract.PushTokenRegistration
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import java.io.IOException

/**
 * Keeps this phone's push registration in step with the user's choice and the
 * server's offer (docs/23 §4).
 *
 * * Off unless **both** the server offers a provider and the user opted in:
 *   otherwise the SDK is told to stop and the server to forget the token.
 * * A token (first, or rotated) is bound to this device over its own
 *   authenticated session — the body carries only the token.
 * * Failures leave things as they are and are retried on the next connection;
 *   the phone works exactly as without push meanwhile.
 */
class PushRegistrar(
    private val api: () -> ApiClient,
    private val accessToken: () -> String,
    private val enrolled: () -> Boolean,
    private val settings: PushSettings,
    private val client: PushClient,
    private val scope: CoroutineScope,
) {
    sealed interface Status {
        /** Not offered by the server, or not turned on by the user. */
        data object Off : Status

        /** Turned on; the push SDK is obtaining (or has) a token. */
        data object Enabling : Status

        /** The push SDK cannot run here (e.g. no Google Play services). */
        data class Unavailable(
            val reason: String,
        ) : Status

        /** The server could not be reached or refused; retried later. */
        data class Pending(
            val reason: String,
        ) : Status
    }

    @Volatile
    var status: Status = Status.Off
        private set

    /** Reconcile once (on every channel connect, and when the user toggles). Blocking: call off the main thread. */
    @Synchronized
    fun sync(): Status {
        if (!enrolled()) return Status.Off.also { status = it }
        val config: PushClientConfig =
            try {
                api().pushConfig(accessToken())
            } catch (e: IOException) {
                return Status.Pending(e.message ?: "unreachable").also { status = it }
            } catch (ignored: IllegalArgumentException) {
                return Status.Pending("unexpected push config").also { status = it }
            } catch (ignored: EnrollmentLost) {
                return Status.Off.also { status = it }
            }
        settings.serverProvider = config.provider
        val options = config.fcm
        if (config.provider != PushProviderKind.FCM || options == null || !settings.optedIn) {
            return turnOff()
        }
        status = Status.Enabling
        client.enable(
            options,
            onToken = { token -> scope.launch { register(token) } },
            onUnavailable = { reason -> status = Status.Unavailable(reason) },
        )
        return status
    }

    fun setOptedIn(on: Boolean): Status {
        settings.optedIn = on
        return sync()
    }

    /** A token from the SDK: first issue or rotation (`onNewToken`). Blocking. */
    @Synchronized
    fun register(token: String): Status {
        if (!enrolled() || !settings.optedIn || settings.serverProvider != PushProviderKind.FCM) return status
        if (settings.isRegistered(token)) return status
        val registration =
            try {
                PushTokenRegistration(token = token)
            } catch (ignored: IllegalArgumentException) {
                return Status.Unavailable("unexpected token").also { status = it }
            }
        return try {
            api().registerPushToken(accessToken(), registration)
            settings.markRegistered(token)
            status
        } catch (e: IOException) {
            Status.Pending(e.message ?: "unreachable").also { status = it }
        } catch (ignored: EnrollmentLost) {
            Status.Off.also { status = it }
        }
    }

    /** Revocation wiped this phone: stop the SDK and forget everything. The server already dropped the token. */
    @Synchronized
    fun wipe() {
        client.disable()
        settings.wipe()
        status = Status.Off
    }

    private fun turnOff(): Status {
        client.disable()
        if (settings.hasRegistration()) {
            try {
                api().clearPushToken(accessToken())
                settings.markRegistered(null)
            } catch (e: IOException) {
                return Status.Pending(e.message ?: "unreachable").also { status = it }
            } catch (ignored: EnrollmentLost) {
                settings.markRegistered(null)
            }
        }
        return Status.Off.also { status = it }
    }
}
