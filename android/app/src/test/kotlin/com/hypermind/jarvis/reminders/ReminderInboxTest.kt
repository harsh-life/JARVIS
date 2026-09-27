package com.hypermind.jarvis.reminders

import com.hypermind.jarvis.contract.Reminder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ReminderInboxTest {
    private val shown = mutableListOf<Reminder>()
    private var device: String? = "device-a"
    private val inbox = ReminderInbox(deviceId = { device }, notifier = { shown += it }, capacity = 2)

    private fun reminder(
        delivery: String = "d-1",
        to: String = "device-a",
    ) = Reminder(
        deliveryId = delivery,
        jobId = "job-1",
        deviceId = to,
        taskReason = "water the plants",
        scheduledFor = "2026-10-01T03:30:00Z",
    )

    @Test
    fun `a reminder for this device is shown and acknowledged`() {
        assertEquals("d-1", inbox.receive(reminder())?.deliveryId)
        assertEquals(listOf("d-1"), shown.map { it.deliveryId })
    }

    @Test
    fun `a reminder for another device is neither shown nor acknowledged`() {
        assertNull(inbox.receive(reminder(to = "device-b")))
        assertEquals(emptyList<Reminder>(), shown)
    }

    @Test
    fun `without an enrollment nothing is shown`() {
        device = null
        assertNull(inbox.receive(reminder()))
        assertEquals(emptyList<Reminder>(), shown)
    }

    @Test
    fun `a re-sent reminder is shown once and acknowledged every time`() {
        repeat(3) { assertEquals("d-1", inbox.receive(reminder())?.deliveryId) }
        assertEquals(1, shown.size)
    }

    @Test
    fun `the de-duplication memory is bounded`() {
        inbox.receive(reminder("d-1"))
        inbox.receive(reminder("d-2"))
        inbox.receive(reminder("d-3"))
        inbox.receive(reminder("d-1"))
        assertEquals(listOf("d-1", "d-2", "d-3", "d-1"), shown.map { it.deliveryId })
    }
}
