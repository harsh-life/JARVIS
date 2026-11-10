package com.hypermind.jarvis.auth

import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * OD-CHAR-4: the server's activity report for this user (`GET /presentation/activity`) —
 * presentation only, read-only, owner-scoped by the session. The body is returned as text, at most
 * [MAX_BODY] bytes, for its one consumer to validate; any non-200 answer is null. Nothing here
 * interprets, stores or logs it.
 */
class ActivityApi(
    private val http: OkHttpClient,
    private val api: (String) -> HttpUrl,
) {
    fun report(accessToken: String): String? =
        http
            .newCall(
                Request
                    .Builder()
                    .url(api("presentation/activity"))
                    .header("Authorization", "Bearer $accessToken")
                    .get()
                    .build(),
            ).execute()
            .use { response -> if (response.code == OK) response.peekBody(MAX_BODY).string() else null }

    companion object {
        /** An activity report is a few hundred bytes; anything far larger is not one. */
        const val MAX_BODY = 4_096L
        private const val OK = 200
    }
}
