package com.hypermind.jarvis.presentation.character

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** OD-CHAR-2: the central controller's timing rules, on a virtual clock. */
class CharacterControllerTest {
    private val settings =
        CharacterSettings(
            minDisplayMillis = 1_000,
            idleTimeoutMillis = 60_000,
            interruptScore = 9,
            holdMillis = CharacterSettings.DEFAULT_HOLDS + (Expression.SURPRISED to 2_000L),
        )
    private var now = 0L
    private val controller = CharacterController(settings, now)

    private fun at(time: Long): CharacterController {
        now = time
        controller.tick(now)
        return controller
    }

    private fun select(
        expression: Expression,
        score: Int = 8,
    ) = controller.onSelection(Selection(expression, score), now)

    @Test
    fun `it starts at default, the first-launch state`() {
        assertEquals(Expression.DEFAULT, controller.shown)
        assertEquals(Expression.DEFAULT, at(59_999).shown)
    }

    @Test
    fun `prolonged inactivity becomes sleepy, and activity wakes it at once`() {
        assertEquals(60_000L, controller.nextChangeAt())
        assertEquals(Expression.SLEEPY, at(60_000).shown)
        assertNull("sleepy waits for input", controller.nextChangeAt())
        controller.onActivity(now)
        assertEquals(Expression.DEFAULT, controller.shown)
        assertEquals(Expression.DEFAULT, at(now + 59_999).shown)
        assertEquals(Expression.SLEEPY, at(now + 1).shown)
    }

    @Test
    fun `activity restarts the idle timer`() {
        at(50_000)
        controller.onActivity(now)
        assertEquals(Expression.DEFAULT, at(100_000).shown)
        assertEquals(Expression.SLEEPY, at(110_000).shown)
    }

    @Test
    fun `a selected state shows at once from the baseline and returns to it after its hold`() {
        at(10_000)
        select(Expression.HAPPY)
        assertEquals(Expression.HAPPY, controller.shown)
        assertEquals(Expression.HAPPY, at(14_999).shown)
        assertEquals(Expression.DEFAULT, at(15_000).shown)
    }

    @Test
    fun `no transient state stays indefinitely`() {
        for (state in listOf(
            Expression.HAPPY,
            Expression.SATISFIED,
            Expression.SURPRISED,
            Expression.ANGRY,
            Expression.SAD,
        )) {
            controller.onActivity(now)
            select(state)
            assertEquals(state, controller.shown)
            assertEquals(state.name, Expression.DEFAULT, at(now + settings.hold(state)).shown)
        }
    }

    @Test
    fun `surprise is brief`() {
        select(Expression.SURPRISED)
        assertEquals(Expression.SURPRISED, at(1_999).shown)
        assertEquals(Expression.DEFAULT, at(2_000).shown)
    }

    @Test
    fun `after a long idle a selected state returns to sleepy, not default`() {
        at(70_000)
        assertEquals(Expression.SLEEPY, controller.shown)
        select(Expression.SATISFIED) // e.g. a task finished while the user was away
        assertEquals(Expression.SATISFIED, controller.shown)
        assertEquals(Expression.SLEEPY, at(75_000).shown)
    }

    @Test
    fun `insufficient evidence changes nothing`() {
        select(Expression.SAD)
        controller.onSelection(Selection(Expression.DEFAULT, 0), now)
        assertEquals(Expression.SAD, controller.shown)
    }

    @Test
    fun `a new state waits out the minimum display, and rapid predictions collapse to the latest`() {
        select(Expression.HAPPY)
        at(200)
        select(Expression.SAD)
        at(400)
        select(Expression.ANGRY)
        at(600)
        select(Expression.SURPRISED)
        assertEquals(Expression.HAPPY, controller.shown)
        assertEquals(1_000L, controller.nextChangeAt())
        assertEquals(Expression.HAPPY, at(999).shown)
        assertEquals(Expression.SURPRISED, at(1_000).shown)
        // SAD and ANGRY never appeared: one change, no flicker.
        assertEquals(Expression.DEFAULT, at(3_000).shown)
    }

    @Test
    fun `an urgent state interrupts at once`() {
        select(Expression.HAPPY)
        at(100)
        select(Expression.SURPRISED, score = 9)
        assertEquals(Expression.SURPRISED, controller.shown)
    }

    @Test
    fun `the same state again extends its hold rather than flickering`() {
        select(Expression.HAPPY)
        at(4_000)
        select(Expression.HAPPY)
        assertEquals(Expression.HAPPY, at(8_999).shown)
        assertEquals(Expression.DEFAULT, at(9_000).shown)
    }

    @Test
    fun `a waiting state is not held up behind a hold that ends first`() {
        val short =
            CharacterController(
                settings.copy(
                    minDisplayMillis = 5_000,
                    holdMillis =
                        settings.holdMillis + (Expression.SURPRISED to 1_000L),
                ),
                0,
            )
        short.onSelection(Selection(Expression.SURPRISED, 8), 0)
        short.onSelection(Selection(Expression.HAPPY, 8), 100)
        short.tick(1_000)
        assertEquals(Expression.HAPPY, short.shown)
    }

    @Test
    fun `task completion shows satisfied, then the baseline`() {
        select(Expression.SATISFIED)
        assertEquals(Expression.SATISFIED, controller.shown)
        assertEquals(Expression.DEFAULT, at(5_000).shown)
    }

    @Test
    fun `every state can follow every other, including back to default`() {
        for (from in Expression.entries) {
            for (to in Expression.entries) {
                if (from == to) continue
                val c = CharacterController(settings, 0)
                reach(c, from)
                val t = 10 * 60_000L + 1 // after anything [reach] did for `from`
                reach(c, to, t)
                assertEquals("$from -> $to", to, c.shown)
            }
        }
    }

    /** Drives [c] to [state] the way the app would: a selection, the idle policy, or a return to baseline. */
    private fun reach(
        c: CharacterController,
        state: Expression,
        start: Long = 0,
    ) {
        when (state) {
            Expression.DEFAULT -> {
                c.tick(start + 60_000) // any hold is over …
                c.onActivity(start + 60_000) // … and activity wakes from idle
            }
            Expression.SLEEPY -> c.tick(start + 10 * 60_000)
            else -> c.onSelection(Selection(state, 10), start + 1)
        }
    }

    @Test
    fun `a preview pins a state until reset, and selections wait`() {
        controller.pin(Expression.ANGRY, now)
        select(Expression.HAPPY, score = 10)
        assertEquals(Expression.ANGRY, at(600_000).shown)
        assertNull(controller.nextChangeAt())
        controller.unpin(now)
        assertEquals(Expression.SLEEPY, controller.shown)
    }

    // --- intensity: committed with the expression ------------------------------------------

    private fun presented(
        expression: Expression,
        intensity: Int,
    ) = PresentedCharacter(expression, intensity)

    @Test
    fun `the baselines take their fixed intensities, never a score`() {
        assertEquals(presented(Expression.DEFAULT, 5), controller.presented)
        assertEquals(presented(Expression.SLEEPY, 2), at(60_000).presented)
        controller.onActivity(now)
        assertEquals("waking: default at its own intensity", presented(Expression.DEFAULT, 5), controller.presented)
    }

    @Test
    fun `a selected state's intensity comes from its score and its threshold`() {
        select(Expression.HAPPY, score = 8) // threshold 7
        assertEquals(presented(Expression.HAPPY, 6), controller.presented)
        assertEquals("then the baseline, at its fixed intensity", presented(Expression.DEFAULT, 5), at(5_000).presented)
        select(Expression.SAD, score = 8) // threshold 8: at the floor
        assertEquals(presented(Expression.SAD, 4), controller.presented)
    }

    @Test
    fun `a selection below its own threshold changes nothing`() {
        select(Expression.ANGRY, score = 7) // threshold 8: the selector would never choose it
        assertEquals(presented(Expression.DEFAULT, 5), controller.presented)
        select(Expression.ANGRY, score = 11)
        assertEquals(presented(Expression.DEFAULT, 5), controller.presented)
    }

    @Test
    fun `the same state again keeps its intensity inside the dead band and moves outside it`() {
        select(Expression.HAPPY, score = 8) // 6
        at(500)
        select(Expression.HAPPY, score = 7) // 4: two away, replaces
        assertEquals(presented(Expression.HAPPY, 4), controller.presented)
        at(900)
        select(Expression.HAPPY, score = 8) // 6: two away, replaces
        assertEquals(presented(Expression.HAPPY, 6), controller.presented)
        val narrow = CharacterController(settings.copy(intensityDeadBand = 3), 0)
        narrow.onSelection(Selection(Expression.HAPPY, 8), 0) // 6
        narrow.onSelection(Selection(Expression.HAPPY, 9), 100) // 8: inside a band of 3
        assertEquals(presented(Expression.HAPPY, 6), narrow.presented)
        narrow.onSelection(Selection(Expression.HAPPY, 10), 200) // 10: outside
        assertEquals(presented(Expression.HAPPY, 10), narrow.presented)
    }

    @Test
    fun `a threshold-7 state at score 7 then 8 moves from 4 to 6 - the dead band is at least 2`() {
        select(Expression.HAPPY, score = 7) // threshold 7: at the floor
        assertEquals(presented(Expression.HAPPY, 4), controller.presented)
        at(300)
        select(Expression.HAPPY, score = 8) // 6: exactly the dead band away, so it moves
        assertEquals(presented(Expression.HAPPY, 6), controller.presented)
        // With the default floor every score step of a threshold-7 or -8 state moves at least 2,
        // so the default band of 2 never holds back a real change of score.
        for (threshold in listOf(7, 8)) {
            val steps = (threshold..10).map { IntensityPolicy.forSelection(it, threshold) }
            assertTrue("$steps", steps.zipWithNext().all { (a, b) -> b - a >= IntensityPolicy.DEFAULT_DEAD_BAND })
        }
    }

    @Test
    fun `a waiting candidate carries its own intensity and goes up with it`() {
        select(Expression.HAPPY, score = 10)
        at(200)
        select(Expression.SAD, score = 8)
        at(400)
        select(Expression.SURPRISED, score = 8) // the latest wins, at its own intensity
        assertEquals(presented(Expression.HAPPY, 10), controller.presented)
        assertEquals(presented(Expression.SURPRISED, 6), at(1_000).presented)
    }

    @Test
    fun `an urgent interruption commits the new state with its own intensity`() {
        select(Expression.HAPPY, score = 7)
        at(100)
        select(Expression.SURPRISED, score = 9)
        assertEquals(presented(Expression.SURPRISED, 8), controller.presented)
    }

    @Test
    fun `a selected sleepy differs from the idle sleepy`() {
        select(Expression.SLEEPY, score = 9) // threshold 8
        assertEquals(presented(Expression.SLEEPY, 7), controller.presented)
        assertEquals(presented(Expression.DEFAULT, 5), at(5_000).presented)
        assertEquals(presented(Expression.SLEEPY, 2), at(65_000).presented)
    }

    @Test
    fun `a preview pins at intensity 5 unless another is chosen`() {
        controller.pin(Expression.SAD, now)
        assertEquals(presented(Expression.SAD, 5), controller.presented)
        controller.pin(Expression.SAD, now, intensity = 10)
        assertEquals(presented(Expression.SAD, 10), controller.presented)
        controller.unpin(now)
        assertEquals(presented(Expression.DEFAULT, 5), controller.presented)
    }

    @Test
    fun `baseline intensities follow the settings`() {
        val baselines = mapOf(Expression.DEFAULT to 6, Expression.SLEEPY to 3)
        val tuned = CharacterController(settings.copy(baselineIntensities = baselines), 0)
        assertEquals(presented(Expression.DEFAULT, 6), tuned.presented)
        tuned.tick(60_000)
        assertEquals(presented(Expression.SLEEPY, 3), tuned.presented)
    }
}
