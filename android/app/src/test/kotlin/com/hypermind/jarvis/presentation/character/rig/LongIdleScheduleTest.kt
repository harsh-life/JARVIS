package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The application-owned long-idle timer: ten minutes of one unchanged, visible state. */
class LongIdleScheduleTest {
    private val period = LongIdleSchedule.DEFAULT_PERIOD_MILLIS
    private val schedule = LongIdleSchedule()

    @Test
    fun `the default period is ten minutes`() {
        assertEquals(600_000L, period)
    }

    @Test
    fun `nothing is due before a state is shown`() {
        assertNull(schedule.nextAt())
        assertFalse(schedule.due(Long.MAX_VALUE))
    }

    @Test
    fun `due after ten minutes of one state, not before`() {
        schedule.observe(Expression.DEFAULT, 1_000)
        assertEquals(1_000 + period, schedule.nextAt())
        assertFalse(schedule.due(1_000 + period - 1))
        assertTrue(schedule.due(1_000 + period))
    }

    @Test
    fun `a change of state restarts the count, the same state does not`() {
        schedule.observe(Expression.DEFAULT, 0)
        schedule.observe(Expression.DEFAULT, period / 2)
        assertEquals(period, schedule.nextAt())
        schedule.observe(Expression.HAPPY, period / 2)
        assertFalse(schedule.due(period))
        assertTrue(schedule.due(period / 2 + period))
    }

    @Test
    fun `after firing, the next one is a full period later`() {
        schedule.observe(Expression.SLEEPY, 0)
        assertTrue(schedule.due(period))
        schedule.fired(period)
        assertFalse(schedule.due(period + 1))
        assertTrue(schedule.due(2 * period))
    }

    @Test
    fun `time in the background does not count`() {
        schedule.observe(Expression.DEFAULT, 0)
        schedule.pause()
        assertNull(schedule.nextAt())
        assertFalse(schedule.due(10 * period))
        schedule.resume(10 * period)
        assertFalse(schedule.due(10 * period + period - 1))
        assertTrue(schedule.due(11 * period))
    }

    @Test
    fun `a period must be positive`() {
        assertTrue(runCatching { LongIdleSchedule(0) }.isFailure)
    }
}
