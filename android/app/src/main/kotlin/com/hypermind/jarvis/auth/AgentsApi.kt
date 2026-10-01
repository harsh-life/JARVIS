package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.AgentList
import com.hypermind.jarvis.contract.AgentRunResponse
import com.hypermind.jarvis.contract.RunAgentRequest
import com.hypermind.jarvis.contract.TaskView
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * The owner's agents over the Track B API (docs/29 §23.2, Phase 4) — parsed
 * strictly by [AgentList] and [AgentRunResponse]. The server derives the owner,
 * graph, agent version and everything a run may do from the session and the
 * stored spec; nothing here sends any of it.
 */
class AgentsApi(
    private val http: OkHttpClient,
    private val api: (String) -> HttpUrl,
) {
    fun listAgents(accessToken: String): AgentList =
        http
            .newCall(
                Request
                    .Builder()
                    .url(api("agents"))
                    .header("Authorization", "Bearer $accessToken")
                    .get()
                    .build(),
            ).execute()
            .use { response -> AgentList.parse(response.code, response.body?.string().orEmpty()) }

    /**
     * Ask the server to run one of the owner's agents now, as this session
     * (docs/29 §7.4). From a reminder tap, the body names that delivery — the
     * server checks it against this device and makes the same tap one run.
     */
    fun runAgent(
        accessToken: String,
        agentId: String,
        reminderDeliveryId: String?,
    ): TaskView =
        http
            .newCall(
                Request
                    .Builder()
                    .url(api("agents/$agentId/runs"))
                    .header("Authorization", "Bearer $accessToken")
                    .post(RunAgentRequest.encode(reminderDeliveryId).toRequestBody(JSON))
                    .build(),
            ).execute()
            .use { response -> AgentRunResponse.parse(response.code, response.body?.string().orEmpty()) }

    private companion object {
        val JSON = "application/json".toMediaType()
    }
}
