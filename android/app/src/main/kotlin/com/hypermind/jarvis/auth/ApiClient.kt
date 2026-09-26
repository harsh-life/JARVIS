package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.ContractJson
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
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

        /** The server URL must be a bare https origin — never cleartext. */
        fun validServerUrl(url: String): String? {
            val parsed = url.trim().trimEnd('/').toHttpUrlOrNull() ?: return null
            if (parsed.scheme != "https" || parsed.encodedPath != "/" || parsed.query != null) return null
            return "https://${parsed.host}" + if (parsed.port != 443) ":${parsed.port}" else ""
        }
    }
}
