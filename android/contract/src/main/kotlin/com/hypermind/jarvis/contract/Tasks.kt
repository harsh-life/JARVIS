@file:UseSerializers(StrictIntSerializer::class, StrictBooleanSerializer::class)

package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

/**
 * The agent-task API as this client reads it (02 §5) — a strict mirror of
 * `AgentResult`/`PendingAction` in `shared/schemas/agent.py`, held to
 * `shared/android/task_samples.json`. Nothing here is authority: a result is
 * what the server decided; the device only displays it and, for a pending
 * action, posts the user's own answer back.
 */
@Serializable
enum class TaskStatus {
    @SerialName("running")
    RUNNING,

    @SerialName("awaiting_confirmation")
    AWAITING_CONFIRMATION,

    @SerialName("waiting_for_platform")
    WAITING_FOR_PLATFORM,

    @SerialName("completed")
    COMPLETED,

    @SerialName("failed")
    FAILED,

    @SerialName("cancelled")
    CANCELLED,
}

@Serializable
enum class RiskCategory {
    @SerialName("low_read")
    LOW_READ,

    @SerialName("low_write")
    LOW_WRITE,

    @SerialName("consequential")
    CONSEQUENTIAL,

    @SerialName("high_irreversible")
    HIGH_IRREVERSIBLE,
}

@Serializable
data class PendingAction(
    val kind: String,
    val capability: String,
    @SerialName("risk_category") val riskCategory: RiskCategory,
    @SerialName("tool_id") val toolId: String? = null,
    val operation: String? = null,
    @SerialName("resource_ref") val resourceRef: String? = null,
    val arguments: JsonObject = JsonObject(emptyMap()),
    @SerialName("resource_scope") val resourceScope: Map<String, String>? = null,
    @SerialName("requires_step_up") val requiresStepUp: Boolean = false,
    // Absent from a replayed response (only its hash is stored server-side);
    // then fetched from GET /agent/tasks/{id}.
    @SerialName("confirmation_token") val confirmationToken: String? = null,
    @SerialName("expires_at") val expiresAt: String,
) {
    override fun toString(): String = "PendingAction($capability.$operation, $riskCategory)"
}

@Serializable
data class PlatformWait(
    val dependency: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("expires_at") val expiresAt: String,
)

@Serializable
data class TaskFailure(
    val code: String,
    val message: String,
)

@Serializable
data class TaskCounters(
    val iterations: Int = 0,
    @SerialName("model_calls") val modelCalls: Int = 0,
    @SerialName("tool_calls") val toolCalls: Int = 0,
    @SerialName("worker_switches") val workerSwitches: Int = 0,
)

@Serializable
data class AgentResult(
    @SerialName("task_id") val taskId: String,
    val status: TaskStatus,
    val mode: String = "execute",
    val response: String? = null,
    val unresolved: Boolean = false,
    val failure: TaskFailure? = null,
    val pending: PendingAction? = null,
    @SerialName("waiting_for") val waitingFor: PlatformWait? = null,
    @SerialName("active_capabilities") val activeCapabilities: List<String> = emptyList(),
    val notes: List<String> = emptyList(),
    val counters: TaskCounters = TaskCounters(),
    @SerialName("break_glass_active") val breakGlassActive: Boolean = false,
)

/** One agent-task HTTP response, as the device acts on it. */
sealed interface TaskView {
    data class Result(
        val result: AgentResult,
    ) : TaskView

    data class NeedsConfirmation(
        val taskId: String,
        val pending: PendingAction,
    ) : TaskView

    data class Failed(
        val taskId: String?,
        val code: String,
        val message: String,
    ) : TaskView

    companion object {
        /** Parse a response strictly; anything unexpected is a failure, never a guess. */
        fun parse(
            httpStatus: Int,
            body: String,
        ): TaskView =
            try {
                val root = ContractJson.parseToJsonElement(body).jsonObject
                val error = root["error"] as? JsonObject
                if (httpStatus in SUCCESS && error == null) {
                    Result(ContractJson.decodeFromJsonElement(AgentResult.serializer(), root))
                } else {
                    fromError(error)
                }
            } catch (ignored: IllegalArgumentException) {
                Failed(null, "malformed_response", "The server's answer could not be read.")
            }

        private fun fromError(error: JsonObject?): TaskView {
            error ?: return Failed(null, "malformed_response", "The server's answer could not be read.")
            val code = (error["code"] as? JsonPrimitive)?.content ?: "unknown"
            val message = (error["message"] as? JsonPrimitive)?.content ?: ""
            val details = error["details"] as? JsonObject
            val taskId = details?.string("task_id")
            val pending = details?.get("pending")
            if (code == "confirmation_required" && taskId != null && pending is JsonObject) {
                return NeedsConfirmation(
                    taskId,
                    ContractJson.decodeFromJsonElement(PendingAction.serializer(), pending),
                )
            }
            return Failed(taskId, details?.string("failure_code") ?: code, message)
        }

        private fun JsonObject.string(key: String): String? =
            (get(key) as? JsonPrimitive)?.takeUnless { it is JsonNull }?.content

        private val SUCCESS = 200..299
    }
}

/**
 * The confirmation screen's text (docs/23 §5.4, ANDC-T10): built **only** from
 * the server's canonical pending action — capability, operation, target app,
 * exact arguments, risk tier. Never from model prose (`response`, `notes`) or
 * anything else a worker wrote, so no model can phrase its own approval.
 */
object ConfirmationText {
    data class Screen(
        val title: String,
        val lines: List<String>,
        val warning: String?,
        val needsStepUp: Boolean,
    )

    fun of(pending: PendingAction): Screen {
        val lines = mutableListOf<String>()
        lines += "Action: ${pending.capability}.${pending.operation ?: pending.kind}"
        pending.resourceScope?.get("package_name")?.let { lines += "In app: $it" }
        pending.resourceScope
            ?.filterKeys { it != "package_name" }
            ?.forEach { (key, value) -> lines += "Limited to $key: $value" }
        pending.resourceRef?.let { lines += "On: $it" }
        pending.toolId?.let { lines += "Tool: $it" }
        if (pending.arguments.isEmpty()) {
            lines += "With no arguments"
        } else {
            pending.arguments.forEach { (key, value) -> lines += "$key: ${literal(value)}" }
        }
        val warning =
            when (pending.riskCategory) {
                RiskCategory.HIGH_IRREVERSIBLE -> "This cannot be undone. You will be asked to verify it is you."
                RiskCategory.CONSEQUENTIAL -> "This acts on your behalf."
                else -> null
            }
        return Screen(
            title = "Approve this action?",
            lines = lines,
            warning = warning,
            needsStepUp = pending.requiresStepUp || pending.riskCategory == RiskCategory.HIGH_IRREVERSIBLE,
        )
    }

    /** Exact values, quoted — what will be sent is what is shown. */
    private fun literal(value: JsonElement): String = value.toString()
}
