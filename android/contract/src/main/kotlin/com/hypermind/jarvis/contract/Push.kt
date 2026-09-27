package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Push wake (docs/23 §4, ANDC-T9) — the device's side of `shared/schemas/push.py`,
 * held to `shared/android/push_samples.json`.
 *
 * A push is a request to reconnect and nothing more. The phone acts on a data
 * map only if it is **exactly** `{"type": "wake"}`; a map that merely contains
 * it, or carries anything else, is ignored whole. Nothing in a push is ever
 * read as an operation, a task, a confirmation or an identity.
 */
object WakeSignal {
    const val TYPE = "wake"

    fun isWake(data: Map<String, String>): Boolean = data.size == 1 && data["type"] == TYPE
}

@Serializable
enum class PushProviderKind {
    @SerialName("none")
    NONE,

    @SerialName("fcm")
    FCM,
}

/** The public Firebase client identifiers the server hands an enrolled phone. */
@Serializable
data class FcmClientOptions(
    @SerialName("project_id") val projectId: String,
    @SerialName("application_id") val applicationId: String,
    @SerialName("api_key") val apiKey: String,
    @SerialName("sender_id") val senderId: String,
)

/** `GET /devices/push-config`. `none`: the phone never initializes a push SDK. */
@Serializable
data class PushClientConfig(
    val provider: PushProviderKind,
    val fcm: FcmClientOptions? = null,
) {
    init {
        require((provider == PushProviderKind.FCM) == (fcm != null)) { "fcm options exactly when the provider is fcm" }
    }
}

/**
 * `PUT /devices/me/push-token` — this device's own token. No device, user or
 * graph id: the server binds it to the authenticated caller.
 */
@Serializable
data class PushTokenRegistration(
    val provider: String = "fcm",
    val token: String,
) {
    init {
        require(provider == "fcm") { "unknown push provider" }
        require(TOKEN.matches(token)) { "not a registration token" }
    }

    override fun toString(): String = "PushTokenRegistration($provider, <redacted>)"

    companion object {
        /** `FCM_TOKEN_PATTERN` in shared/schemas/push.py. */
        val TOKEN = Regex("^[A-Za-z0-9_:\\-]{32,4096}$")
    }
}
