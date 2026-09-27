@file:UseSerializers(StrictBooleanSerializer::class)

package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers

/**
 * Reminders over the device channel (docs/22 §2) — a strict mirror of
 * `DeviceReminder` / `DeviceReminderAck` / `ChannelFeature` in
 * `shared/schemas/device_channel.py`.
 *
 * A [Reminder] is a message for the user, never an instruction to the device:
 * it has no capability, operation, primitive or arguments, so there is nothing
 * in it this client could execute. Showing it is all the device does; "start
 * task" only puts [Reminder.taskReason] in the task box for the user to send.
 */
@Serializable
enum class ChannelFeature {
    @SerialName("reminders")
    REMINDERS,
}

@Serializable
data class Reminder(
    val type: String = "reminder",
    @SerialName("delivery_id") val deliveryId: String,
    @SerialName("job_id") val jobId: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("task_reason") val taskReason: String,
    @SerialName("scheduled_for") val scheduledFor: String,
    val late: Boolean = false,
    val recurring: Boolean = false,
) {
    init {
        require(type == "reminder") { "not a reminder" }
        require(taskReason.isNotBlank() && taskReason.length <= MAX_REMINDER_TEXT) { "reminder text out of bounds" }
    }

    // The user's own words must never reach a log line through toString.
    override fun toString(): String = "Reminder(deliveryId=$deliveryId, late=$late)"

    companion object {
        const val MAX_REMINDER_TEXT = 8000
    }
}

@Serializable
data class ReminderAck(
    val type: String = "reminder_ack",
    @SerialName("delivery_id") val deliveryId: String,
)

fun ReminderAck.encode(): String = ContractJson.encodeToString(ReminderAck.serializer(), this)
