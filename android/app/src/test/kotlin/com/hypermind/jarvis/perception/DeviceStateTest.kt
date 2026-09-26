package com.hypermind.jarvis.perception

import android.os.BatteryManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DeviceStateTest {
    @Test
    fun `battery state is a percentage with a closed plug vocabulary`() {
        val state =
            BatteryPrimitive.batteryState(
                level = 41,
                scale = 50,
                status = BatteryManager.BATTERY_STATUS_CHARGING,
                plugged = BatteryManager.BATTERY_PLUGGED_USB,
            )!!
        assertEquals(82, state.levelPercent)
        assertEquals(true, state.charging)
        assertEquals("usb", state.plugged)
        assertEquals(
            "none",
            BatteryPrimitive.batteryState(10, 100, BatteryManager.BATTERY_STATUS_DISCHARGING, 0)!!.plugged,
        )
        assertNull(BatteryPrimitive.batteryState(-1, 100, 0, 0))
    }

    @Test
    fun `notifications are the named app's only, newest first, bounded`() {
        val posted =
            listOf(
                PostedNotification("com.example.notes", "Old", "a", 1_000),
                PostedNotification("com.bank.app", "OTP", "123456", 3_000),
                PostedNotification("com.example.notes", "New", "b\nc", 2_000),
            )
        val list = NotificationPrimitive.select(posted, "com.example.notes", limit = 10)
        assertEquals(listOf("New", "Old"), list.notifications.map { it.title })
        assertEquals("1970-01-01T00:00:02Z", list.notifications.first().postedAt)
        assertEquals(1, NotificationPrimitive.select(posted, "com.example.notes", limit = 1).notifications.size)
        assertEquals(
            emptyList<String>(),
            NotificationPrimitive.select(posted, "com.other.app", 10).notifications.map {
                it.title
            },
        )
    }
}
