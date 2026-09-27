package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.GrantList
import com.hypermind.jarvis.contract.GrantRequest
import com.hypermind.jarvis.contract.PushClientConfig
import com.hypermind.jarvis.contract.PushTokenRegistration
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.contract.VoiceConfigView
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.time.Instant
import java.util.UUID

/** An error from the Track B API, in its canonical envelope (02 §1.6/§1.7). */
class ApiException(
    val status: Int,
    val code: String,
) : IOException("api error $status $code")

/**
 * The Track B HTTP API — only the calls the device itself makes (02 §3). The
 * server derives every identity fact from the credential it verifies; nothing
 * here sends a user id, graph, or capability claim (PHONE-003).
 */
class ApiClient(
    private val http: OkHttpClient,
    private val baseUrl: String,
) {
    private val lenient = Json { ignoreUnknownKeys = true }

    @Serializable
    data class Registered(
        @SerialName("device_id") val deviceId: String,
    )

    @Serializable
    data class Token(
        @SerialName("access_token") val accessToken: String,
        @SerialName("expires_at") val expiresAt: String,
    ) {
        override fun toString() = "Token(<redacted>, expiresAt=$expiresAt)"
    }

    /** `/auth/oidc/start` with this app's login nonce — returns the Google URL to open. */
    fun oidcStartUrl(appState: String): String {
        val url = api("auth/oidc/start").newBuilder().addQueryParameter("app_state", appState).build()
        val body =
            execute(
                Request
                    .Builder()
                    .url(url)
                    .get()
                    .build(),
            )
        return body.getValue("redirect_url").jsonPrimitive.content
    }

    /** Register this device's public key with the one-time bootstrap token (03 §3.1). */
    fun register(
        bootstrapToken: String,
        publicKey: String,
        keyProof: String,
    ): UUID {
        val payload =
            ContractJson.encodeToString(
                RegisterRequest.serializer(),
                RegisterRequest(publicKey = publicKey, keyProof = keyProof),
            )
        val body =
            execute(
                Request
                    .Builder()
                    .url(api("devices"))
                    .header("Authorization", "Bearer $bootstrapToken")
                    .post(payload.toRequestBody(JSON))
                    .build(),
            )
        return UUID.fromString(lenient.decodeFromJsonElement(Registered.serializer(), body).deviceId)
    }

    /** A short-lived access token for a fresh device proof (03 §5.1). */
    fun token(proof: String): Token {
        val payload = ContractJson.encodeToString(TokenRequest.serializer(), TokenRequest(proof))
        val body =
            execute(
                Request
                    .Builder()
                    .url(api("sessions/token"))
                    .post(payload.toRequestBody(JSON))
                    .build(),
            )
        return lenient.decodeFromJsonElement(Token.serializer(), body)
    }

    /** Remove this device (03 §4.4) — the owner's lost-phone path, also "sign out here". */
    fun revoke(
        deviceId: UUID,
        accessToken: String,
    ) {
        execute(
            Request
                .Builder()
                .url(api("devices/$deviceId"))
                .header("Authorization", "Bearer $accessToken")
                .delete()
                .build(),
            expectBody = false,
        )
    }

    /** The server's sensitive-app classification, for the device to cache (docs/23 §5.2). */
    fun appPolicy(accessToken: String): AppPolicy {
        val body =
            execute(
                Request
                    .Builder()
                    .url(api("devices/app-policy"))
                    .header("Authorization", "Bearer $accessToken")
                    .get()
                    .build(),
            )
        return lenient.decodeFromJsonElement(AppPolicy.serializer(), body)
    }

    /** docs/23 §3: register the step-up key's public half (SPKI) — enrollment only. */
    fun registerStepUpKey(
        accessToken: String,
        publicKey: String,
    ) {
        execute(
            Request
                .Builder()
                .url(api("devices/me/step-up-key"))
                .header("Authorization", "Bearer $accessToken")
                .post(buildJsonObject { put("public_key", publicKey) }.toString().toRequestBody(JSON))
                .build(),
            expectBody = false,
        )
    }

    /** A single-use step-up challenge. */
    fun stepUpChallenge(accessToken: String): String =
        execute(
            Request
                .Builder()
                .url(api("sessions/step-up/challenge"))
                .header("Authorization", "Bearer $accessToken")
                .post(ByteArray(0).toRequestBody(JSON))
                .build(),
        ).getValue("challenge").jsonPrimitive.content

    /** The signed challenge; returns until when this device counts as re-attested. */
    fun stepUpAttest(
        accessToken: String,
        challenge: String,
        signature: String,
    ): Instant {
        val body =
            execute(
                Request
                    .Builder()
                    .url(api("sessions/step-up"))
                    .header("Authorization", "Bearer $accessToken")
                    .post(
                        buildJsonObject {
                            put("challenge", challenge)
                            put("signature", signature)
                        }.toString().toRequestBody(JSON),
                    ).build(),
            )
        return java.time.OffsetDateTime
            .parse(body.getValue("reattested_until").jsonPrimitive.content)
            .toInstant()
    }

    // ── the per-app grid's grants (PRD §13, 02 §6) — parsed strictly ────

    /** This caller's active grants (its user, device and session). */
    fun listGrants(accessToken: String): GrantList =
        ContractJson.decodeFromJsonElement(
            GrantList.serializer(),
            execute(
                Request
                    .Builder()
                    .url(api("capabilities"))
                    .header("Authorization", "Bearer $accessToken")
                    .get()
                    .build(),
            ),
        )

    /** The user's consent to one grid toggle's capability, for this device and one app. */
    fun createGrant(
        accessToken: String,
        request: GrantRequest,
    ) {
        execute(
            Request
                .Builder()
                .url(api("capabilities"))
                .header("Authorization", "Bearer $accessToken")
                .post(ContractJson.encodeToString(GrantRequest.serializer(), request).toRequestBody(JSON))
                .build(),
            expectBody = false,
        )
    }

    /** Revoke a grant; one already gone (`404`) counts as revoked. */
    fun revokeGrant(
        accessToken: String,
        grantId: String,
    ) {
        try {
            execute(
                Request
                    .Builder()
                    .url(api("capabilities/${UUID.fromString(grantId)}"))
                    .header("Authorization", "Bearer $accessToken")
                    .delete()
                    .build(),
                expectBody = false,
            )
        } catch (e: ApiException) {
            if (e.status != NOT_FOUND) throw e
        }
    }

    // ── voice (docs/27) — placement only ───────────────────────────────

    /** Where speech recognition and synthesis run. Strict: a field it does not know is an error. */
    fun voiceConfig(accessToken: String): VoiceConfigView =
        ContractJson.decodeFromJsonElement(
            VoiceConfigView.serializer(),
            execute(
                Request
                    .Builder()
                    .url(api("voice/config"))
                    .header("Authorization", "Bearer $accessToken")
                    .get()
                    .build(),
            ),
        )

    // ── push wake (docs/23 §4) — this device's own registration only ───

    /** Whether the server wakes phones, and the public Firebase ids to do it. Strict. */
    fun pushConfig(accessToken: String): PushClientConfig =
        ContractJson.decodeFromJsonElement(
            PushClientConfig.serializer(),
            execute(
                Request
                    .Builder()
                    .url(api("devices/push-config"))
                    .header("Authorization", "Bearer $accessToken")
                    .get()
                    .build(),
            ),
        )

    /** Bind this device's registration token (on first use and on every rotation). */
    fun registerPushToken(
        accessToken: String,
        registration: PushTokenRegistration,
    ) {
        execute(
            Request
                .Builder()
                .url(api("devices/me/push-token"))
                .header("Authorization", "Bearer $accessToken")
                .put(ContractJson.encodeToString(PushTokenRegistration.serializer(), registration).toRequestBody(JSON))
                .build(),
            expectBody = false,
        )
    }

    /** The user turned push wake off here: the server forgets the token. */
    fun clearPushToken(accessToken: String) {
        execute(
            Request
                .Builder()
                .url(api("devices/me/push-token"))
                .header("Authorization", "Bearer $accessToken")
                .delete()
                .build(),
            expectBody = false,
        )
    }

    // ── agent tasks (02 §5) — responses parsed strictly by TaskView ──────

    /** Submit a task. A fresh Idempotency-Key per submission (02 §1.4). */
    fun submitTask(
        accessToken: String,
        input: String,
        idempotencyKey: String = UUID.randomUUID().toString(),
    ): TaskView =
        raw(
            Request
                .Builder()
                .url(api("agent/tasks"))
                .header("Authorization", "Bearer $accessToken")
                .header("Idempotency-Key", idempotencyKey)
                .post(buildJsonObject { put("input", input) }.toString().toRequestBody(JSON))
                .build(),
        )

    fun getTask(
        accessToken: String,
        taskId: String,
    ): TaskView =
        raw(
            Request
                .Builder()
                .url(api("agent/tasks/$taskId"))
                .header("Authorization", "Bearer $accessToken")
                .get()
                .build(),
        )

    /** The user's own answer to a pending action (docs/23 §5.4) — never an Android-only token. */
    fun confirmTask(
        accessToken: String,
        taskId: String,
        confirmationToken: String,
        approve: Boolean,
    ): TaskView =
        raw(
            Request
                .Builder()
                .url(api("agent/tasks/$taskId/confirm"))
                .header("Authorization", "Bearer $accessToken")
                .post(
                    buildJsonObject {
                        put("confirmation_token", confirmationToken)
                        put("approve", approve)
                    }.toString().toRequestBody(JSON),
                ).build(),
        )

    private fun raw(request: Request): TaskView =
        http.newCall(request).execute().use { response ->
            TaskView.parse(response.code, response.body?.string().orEmpty())
        }

    fun channelUrl(): String =
        api("devices/channel").toString().replaceFirst("https://", "wss://")

    private fun api(path: String): HttpUrl =
        requireNotNull("${baseUrl.trimEnd('/')}/api/v1/$path".toHttpUrlOrNull()) { "invalid server URL" }

    private fun execute(
        request: Request,
        expectBody: Boolean = true,
    ): JsonObject =
        http.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw ApiException(response.code, errorCode(text))
            if (!expectBody) return JsonObject(emptyMap())
            try {
                lenient.parseToJsonElement(text).jsonObject
            } catch (ignored: SerializationException) {
                throw ApiException(response.code, "malformed_response")
            } catch (ignored: IllegalArgumentException) {
                throw ApiException(response.code, "malformed_response")
            }
        }

    private fun errorCode(text: String): String =
        try {
            lenient
                .parseToJsonElement(text)
                .jsonObject["error"]
                ?.jsonObject
                ?.get("code")
                ?.jsonPrimitive
                ?.content ?: "unknown"
        } catch (ignored: SerializationException) {
            "unknown"
        } catch (ignored: IllegalArgumentException) {
            "unknown"
        }

    @Serializable
    private data class RegisterRequest(
        val platform: String = "android",
        @SerialName("public_key") val publicKey: String,
        @SerialName("key_proof") val keyProof: String,
    )

    @Serializable
    private data class TokenRequest(
        @SerialName("device_credential") val deviceCredential: String,
    )

    companion object {
        private val JSON = "application/json".toMediaType()
        private const val NOT_FOUND = 404

        /** The server URL must be a bare https origin — never cleartext. */
        fun validServerUrl(url: String): String? {
            val parsed = url.trim().trimEnd('/').toHttpUrlOrNull() ?: return null
            if (parsed.scheme != "https" || parsed.encodedPath != "/" || parsed.query != null) return null
            return "https://${parsed.host}" + if (parsed.port != 443) ":${parsed.port}" else ""
        }
    }
}
