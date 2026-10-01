@file:UseSerializers(StrictIntSerializer::class, StrictBooleanSerializer::class)

package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * The owner's agents and their runs as this client reads them (docs/29 §23.3,
 * Phase 4) — a strict mirror of `AgentView`, `AgentRunView` and
 * `RunAgentRequest` in `shared/schemas/agent_factory.py`, held to
 * `shared/android/agent_samples.json`.
 *
 * Display only. What an agent may do is decided by the server at every step
 * of every run; this client lists the owner's agents and sends the owner's own
 * "Run" back. A run is shown as the ordinary task it ran as — its pending
 * action confirmed through `/agent/tasks/{id}/confirm` like any task's.
 */
@Serializable
enum class AgentStatus {
    @SerialName("awaiting_confirmation")
    AWAITING_CONFIRMATION,

    @SerialName("active")
    ACTIVE,

    @SerialName("paused")
    PAUSED,

    @SerialName("needs_reapproval")
    NEEDS_REAPPROVAL,

    @SerialName("revoked")
    REVOKED,

    @SerialName("deleted")
    DELETED,
}

@Serializable
enum class AgentRunStatus {
    @SerialName("queued")
    QUEUED,

    @SerialName("running")
    RUNNING,

    @SerialName("waiting")
    WAITING,

    @SerialName("completed")
    COMPLETED,

    @SerialName("failed")
    FAILED,

    @SerialName("cancelled")
    CANCELLED,
}

@Serializable
data class AgentBudget(
    @SerialName("per_run") val perRun: Double,
    @SerialName("per_month") val perMonth: Double,
)

@Serializable
data class AgentSummary(
    @SerialName("agent_id") val agentId: String,
    val name: String,
    val status: AgentStatus,
    @SerialName("current_version") val currentVersion: Int,
    @SerialName("template_id") val templateId: String,
    @SerialName("template_description") val templateDescription: String,
    @SerialName("runtime_display_name") val runtimeDisplayName: String,
    @SerialName("model_profile_display_name") val modelProfileDisplayName: String,
    val can: List<String>,
    val cannot: List<String>,
    @SerialName("trigger_display") val triggerDisplay: String,
    val budget: AgentBudget,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
    @SerialName("last_run") val lastRun: String? = null,
) {
    /** The server will consider a run (it still decides); a paused agent offers none. */
    val runnable: Boolean get() = status == AgentStatus.ACTIVE

    // The owner's own words must never reach a log line through toString.
    override fun toString(): String = "AgentSummary(agentId=$agentId, status=$status)"
}

@Serializable
private data class AgentListBody(
    val items: List<AgentSummary>,
)

/** `GET /agents`: the owner's own agents, or why not. */
sealed interface AgentList {
    data class Shown(
        val agents: List<AgentSummary>,
    ) : AgentList {
        override fun toString(): String = "AgentList.Shown(${agents.size})"
    }

    data class Failed(
        val code: String,
    ) : AgentList

    companion object {
        fun parse(
            httpStatus: Int,
            body: String,
        ): AgentList =
            try {
                if (httpStatus !in SUCCESS) {
                    Failed(errorCode(ContractJson.parseToJsonElement(body).jsonObject))
                } else {
                    Shown(ContractJson.decodeFromString(AgentListBody.serializer(), body).items)
                }
            } catch (ignored: IllegalArgumentException) {
                Failed("malformed_response")
            }
    }
}

@Serializable
private data class AgentRunBody(
    @SerialName("run_id") val runId: String,
    @SerialName("agent_id") val agentId: String,
    val version: Int,
    val kind: String,
    val status: AgentRunStatus,
    @SerialName("failure_code") val failureCode: String? = null,
    @SerialName("task_id") val taskId: String? = null,
    @SerialName("started_at") val startedAt: String,
    @SerialName("finished_at") val finishedAt: String? = null,
    @SerialName("cost_total") val costTotal: Double = 0.0,
    @SerialName("inbox_item_id") val inboxItemId: String? = null,
    val task: AgentResult? = null,
) {
    init {
        require(kind == "on_demand" || kind == "reminder_tap") { "unknown run kind" }
    }
}

/**
 * `POST /agents/{id}/runs`: shown as the task the run is. A run that never
 * became a task (refused before it started) is a failure with the server's
 * reason — never a guess.
 */
object AgentRunResponse {
    fun parse(
        httpStatus: Int,
        body: String,
    ): TaskView =
        try {
            if (httpStatus !in SUCCESS) {
                TaskView.Failed(null, errorCode(ContractJson.parseToJsonElement(body).jsonObject), errorMessage(body))
            } else {
                val run = ContractJson.decodeFromString(AgentRunBody.serializer(), body)
                run.task?.let { TaskView.Result(it) }
                    ?: TaskView.Failed(run.taskId, run.failureCode ?: "not_started", "The agent did not start.")
            }
        } catch (ignored: IllegalArgumentException) {
            TaskView.Failed(null, "malformed_response", "The server's answer could not be read.")
        }
}

/**
 * The body of `POST /agents/{id}/runs`: empty for a run on demand; for a run
 * tapped from one of the agent's reminders, the delivery it came from — which
 * the server checks against this very device and agent. Nothing else.
 */
object RunAgentRequest {
    fun encode(reminderDeliveryId: String?): String =
        buildJsonObject {
            if (reminderDeliveryId != null) put("reminder_delivery_id", reminderDeliveryId)
        }.toString()
}

private val SUCCESS = 200..299

/** The server's reason (`details.reason`), else its failure code, else its error code. */
private fun errorCode(root: JsonObject): String {
    val error = root["error"] as? JsonObject ?: return "malformed_response"
    val details = error["details"] as? JsonObject
    return details?.string("reason")
        ?: details?.string("failure_code")
        ?: (error["code"] as? JsonPrimitive)?.content
        ?: "unknown"
}

private fun errorMessage(body: String): String =
    try {
        val error = ContractJson.parseToJsonElement(body).jsonObject["error"] as? JsonObject
        (error?.get("message") as? JsonPrimitive)?.content.orEmpty()
    } catch (ignored: IllegalArgumentException) {
        ""
    }

private fun JsonObject.string(key: String): String? =
    (get(key) as? JsonPrimitive)?.takeUnless { it is JsonNull }?.content
