package com.hypermind.jarvis.reminders

import android.content.Context
import android.content.Intent
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.MainActivity
import com.hypermind.jarvis.contract.Reminder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * "Start task" reads a reminder's words only from this app's own record — the
 * exported MainActivity cannot be handed text by another app (docs/22 §2).
 */
@RunWith(RobolectricTestRunner::class)
class ReminderDraftsTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val drafts =
        SharedPrefsReminderDrafts(context.getSharedPreferences("drafts-${System.nanoTime()}", Context.MODE_PRIVATE), 2)

    private fun reminder(id: String) =
        Reminder(
            deliveryId = id,
            jobId = "job",
            deviceId = "device-a",
            taskReason = "words of $id",
            scheduledFor = "2026-10-01T03:30:00Z",
        )

    @Test
    fun `a shown reminder's words are recorded under its delivery id`() {
        val inbox = ReminderInbox(deviceId = { "device-a" }, notifier = {}, drafts = drafts)
        inbox.receive(reminder("r-1"))
        assertEquals("words of r-1", drafts.lookup("r-1"))
        inbox.receive(reminder("r-2").copy(deviceId = "device-b"))
        assertNull(drafts.lookup("r-2"))
    }

    @Test
    fun `the record is bounded and wiped on revocation`() {
        drafts.remember("a", "1")
        drafts.remember("b", "2")
        drafts.remember("c", "3")
        assertNull(drafts.lookup("a"))
        assertEquals("3", drafts.lookup("c"))
        drafts.wipe()
        assertNull(drafts.lookup("b"))
        assertNull(drafts.lookup("c"))
    }

    @Test
    fun `the notification's intent carries only the delivery id`() {
        val intent = AndroidReminderNotifier.draftIntent(context, "r-9")
        assertEquals("r-9", intent.getStringExtra(MainActivity.EXTRA_REMINDER_DELIVERY))
        val keys = intent.extras?.keySet().orEmpty()
        assertEquals(setOf(MainActivity.EXTRA_REMINDER_DELIVERY), keys)
        assertFalse(intent.hasExtra("task_draft"))
        assertEquals(MainActivity::class.java.name, intent.component?.className)
        assertEquals(Intent.FLAG_ACTIVITY_NEW_TASK, intent.flags and Intent.FLAG_ACTIVITY_NEW_TASK)
    }

    @Test
    fun `an unknown delivery id yields no draft`() {
        assertNull(drafts.lookup("forged-by-another-app"))
    }
}
