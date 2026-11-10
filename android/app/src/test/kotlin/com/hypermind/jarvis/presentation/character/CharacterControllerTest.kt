package com.hypermind.jarvis.presentation.character

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * OD-CHAR-4: the controller commits what the activity policy decides (the policy's own rules are
 * held to the shared vectors in [ActivityPolicyTest]) and adds only the development preview.
 */
class CharacterControllerTest {
    private val settings = CharacterSettings(idleTimeoutMillis = 60_000)
    private var now = 0L
    private val controller = CharacterController(settings, now)

    private val base = Expression.entries.associateWith { if (it == Expression.DEFAULT) 5 else 1 }

    private fun report(vararg scores: Pair<Expression, Int>) = ActivityReport.of(base + scores)

    private fun at(time: Long): CharacterController {
        now = time
        controller.tick(now)
        return controller
    }

    private fun presented(
        expression: Expression,
        intensity: Int,
    ) = PresentedCharacter(expression, intensity)

    @Test
    fun `it starts at default, the first-launch state`() {
        assertEquals(presented(Expression.DEFAULT, 5), controller.presented)
    }

    @Test
    fun `without reports, prolonged local inactivity is sleepy and activity wakes it`() {
        assertEquals(Expression.DEFAULT, at(59_999).shown)
        assertEquals(presented(Expression.SLEEPY, 2), at(60_000).presented)
        controller.onActivity(now)
        assertEquals(presented(Expression.DEFAULT, 5), controller.presented)
    }

    @Test
    fun `a report is presented with its score's intensity`() {
        controller.onReport(report(Expression.HAPPY to 9), now)
        assertEquals(presented(Expression.HAPPY, 8), controller.presented)
        controller.onReport(report(Expression.ANGRY to 10), 15_000)
        assertEquals(presented(Expression.ANGRY, 10), controller.presented)
    }

    @Test
    fun `a discarded report changes nothing`() {
        controller.onReport(report(Expression.SAD to 8), now)
        controller.onReport(null, 1_000)
        assertEquals(presented(Expression.SAD, 4), controller.presented)
    }

    @Test
    fun `every state can be reached and left again`() {
        var t = 0L
        for (state in Expression.entries - Expression.DEFAULT) {
            t += 120_000 // past every cooldown and the local idle timeout
            controller.onReport(report(state to 10), t)
            assertEquals("$state", state, controller.shown)
            t += 120_000
            controller.onReport(report(), t)
            assertEquals("$state left", Expression.DEFAULT, controller.shown)
        }
    }

    @Test
    fun `a preview pins a state until reset, while reports keep updating underneath`() {
        controller.pin(Expression.ANGRY, now)
        controller.onReport(report(Expression.HAPPY to 10), now)
        assertEquals(presented(Expression.ANGRY, 5), at(30_000).presented)
        assertNull(controller.nextChangeAt(now))
        controller.unpin(now)
        assertEquals(presented(Expression.HAPPY, 10), controller.presented)
    }

    @Test
    fun `a preview can choose its intensity`() {
        controller.pin(Expression.SAD, now, intensity = 10)
        assertEquals(presented(Expression.SAD, 10), controller.presented)
    }

    @Test
    fun `baseline intensities follow the settings`() {
        val tuned =
            CharacterController(
                CharacterSettings(
                    idleTimeoutMillis = 60_000,
                    baselineIntensities = mapOf(Expression.DEFAULT to 6, Expression.SLEEPY to 3),
                ),
                0,
            )
        assertEquals(presented(Expression.DEFAULT, 6), tuned.presented)
        tuned.tick(60_000)
        assertEquals(presented(Expression.SLEEPY, 3), tuned.presented)
    }
}
