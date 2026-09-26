package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

@Serializable
data class ReauthAck(
    val type: String = "reauth_ok",
    @SerialName("session_expires_at") val sessionExpiresAt: String,
)

/** A frame the server sent, parsed strictly. Anything else is [Invalid]. */
sealed interface ServerFrame {
    data class HelloOk(
        val ack: HelloAck,
    ) : ServerFrame

    data class ReauthOk(
        val ack: ReauthAck,
    ) : ServerFrame

    data class Operation(
        val envelope: OperationEnvelope,
    ) : ServerFrame

    data class Cancel(
        val cancel: CancelMessage,
    ) : ServerFrame

    data class Invalid(
        val reason: String,
    ) : ServerFrame

    companion object {
        /**
         * Oversized input is refused before parsing (docs/23 §4 bounds). A
         * frame the device does not understand is never guessed at.
         */
        fun parse(text: String): ServerFrame {
            if (text.length > Bounds.MAX_RESULT_FRAME_BYTES) return Invalid("oversized frame")
            return try {
                val type =
                    ContractJson
                        .parseToJsonElement(text)
                        .jsonObject["type"]
                        ?.jsonPrimitive
                        ?.content
                when (type) {
                    "hello_ok" -> HelloOk(ContractJson.decodeFromString(HelloAck.serializer(), text))
                    "reauth_ok" -> ReauthOk(ContractJson.decodeFromString(ReauthAck.serializer(), text))
                    "operation" -> Operation(ContractJson.decodeFromString(OperationEnvelope.serializer(), text))
                    "cancel" -> parseCancel(text)
                    else -> Invalid("unknown frame type")
                }
            } catch (e: SerializationException) {
                Invalid("malformed frame: ${e.javaClass.simpleName}")
            } catch (e: IllegalArgumentException) {
                Invalid("malformed frame: ${e.javaClass.simpleName}")
            }
        }

        private fun parseCancel(text: String): ServerFrame {
            val cancel = ContractJson.decodeFromString(CancelMessage.serializer(), text)
            return if ((cancel.opId == null) == (cancel.taskId == null)) {
                Invalid("cancel names exactly one target")
            } else {
                Cancel(cancel)
            }
        }
    }
}

fun Hello.encode(): String = ContractJson.encodeToString(Hello.serializer(), this)

fun Reauth.encode(): String = ContractJson.encodeToString(Reauth.serializer(), this)

fun PlatformStatus.encode(): String = ContractJson.encodeToString(PlatformStatus.serializer(), this)

fun ResultEnvelope.encode(): String = ContractJson.encodeToString(ResultEnvelope.serializer(), this)
