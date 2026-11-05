package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The player's contract semantics (export/JarvisCharacter.contract.json): which animation each
 * state change plays, queuing, interruption, long idles, the entrance and exit, pause/resume and
 * release. The expression is always given to it; it never chooses one.
 */
class RigCharacterPlayerTest {
    private val contract = RigAssetsFixture.contract()
    private val runtime = FakeRigRuntime.forContract(contract)
    private val player = RigCharacterPlayer.create(runtime, contract)!!
    private val mix = contract.mix

    private fun started(): RigCharacterPlayer {
        player.start(Expression.DEFAULT, entrance = false)
        runtime.calls.clear()
        return player
    }

    private fun RigCharacterPlayer.run(seconds: Float) {
        var left = seconds
        while (left > 0f) {
            advance(FRAME)
            left -= FRAME
        }
    }

    @Test
    fun `start plays the default idle and the entrance on its own track`() {
        player.start(Expression.DEFAULT, entrance = true)
        assertEquals(listOf("set 0 idle_default loop=true mix=0.0", "set 1 enter loop=false mix=0.0"), runtime.calls)
        assertEquals(Expression.DEFAULT, player.expression)
    }

    @Test
    fun `every state has its own entry, then its idle`() {
        for (target in Expression.entries - Expression.DEFAULT) {
            val fresh = FakeRigRuntime.forContract(contract)
            val p = RigCharacterPlayer.create(fresh, contract)!!
            p.start(Expression.DEFAULT, entrance = false)
            fresh.calls.clear()
            p.show(target)
            assertEquals(
                target.wire,
                listOf(
                    "set 0 to_${target.wire} loop=false mix=${mix.intoEntry}",
                    "add 0 idle_${target.wire} loop=true mix=${mix.toIdle}",
                ),
                fresh.calls,
            )
            assertEquals(target, p.expression)
        }
    }

    @Test
    fun `the five starting states that change the motion use their own transitions`() {
        val pairs =
            mapOf(
                Expression.HAPPY to Expression.SATISFIED,
                Expression.SURPRISED to Expression.DEFAULT,
                Expression.ANGRY to Expression.SATISFIED,
                Expression.SAD to Expression.HAPPY,
                Expression.SLEEPY to Expression.DEFAULT,
            )
        for ((from, to) in pairs) {
            val fresh = FakeRigRuntime.forContract(contract)
            val p = RigCharacterPlayer.create(fresh, contract)!!
            p.start(from, entrance = false)
            fresh.calls.clear()
            p.show(to)
            assertEquals(
                "set 0 trans_${from.wire}_to_${to.wire} loop=false mix=${mix.intoPair}",
                fresh.calls.first(),
            )
            assertEquals("add 0 idle_${to.wire} loop=true mix=${mix.toIdle}", fresh.calls[1])
        }
    }

    @Test
    fun `every other pair uses the target's entry`() {
        val pairs = contract.pairs.keys
        for (from in Expression.entries) {
            for (to in Expression.entries) {
                if (from == to || (from to to) in pairs) continue
                val fresh = FakeRigRuntime.forContract(contract)
                val p = RigCharacterPlayer.create(fresh, contract)!!
                p.start(from, entrance = false)
                fresh.calls.clear()
                p.show(to)
                assertEquals(
                    "${from.wire}>${to.wire}",
                    "set 0 to_${to.wire} loop=false mix=${mix.intoEntry}",
                    fresh.calls.first(),
                )
            }
        }
    }

    @Test
    fun `a repeated identical state never restarts its animation`() {
        val p = started()
        p.show(Expression.HAPPY)
        runtime.calls.clear()
        repeat(5) { p.show(Expression.HAPPY) }
        p.run(2f)
        p.show(Expression.HAPPY)
        assertEquals(emptyList<String>(), runtime.calls)
        p.show(Expression.DEFAULT)
        runtime.calls.clear()
        p.show(Expression.DEFAULT)
        assertEquals(emptyList<String>(), runtime.calls)
    }

    @Test
    fun `a new state during a transition interrupts it and replaces its queued idle`() {
        val p = started()
        p.show(Expression.HAPPY)
        p.run(0.1f)
        runtime.calls.clear()
        p.show(Expression.SAD)
        assertEquals(
            listOf("set 0 to_sad loop=false mix=${mix.interrupt}", "add 0 idle_sad loop=true mix=${mix.toIdle}"),
            runtime.calls,
        )
        p.run(contract.durations.getValue("to_sad") + 0.2f)
        assertEquals("idle_sad", runtime.currentName(0))
    }

    @Test
    fun `rapid changes end on the last state's idle`() {
        val p = started()
        for (e in listOf(Expression.HAPPY, Expression.ANGRY, Expression.SURPRISED, Expression.SAD, Expression.SLEEPY)) {
            p.show(e)
            p.run(0.05f)
        }
        p.run(3f)
        assertEquals(Expression.SLEEPY, p.expression)
        assertEquals("idle_sleepy", runtime.currentName(0))
    }

    @Test
    fun `after its transition the state's idle plays and keeps looping`() {
        val p = started()
        p.show(Expression.ANGRY)
        p.run(contract.durations.getValue("to_angry") + 0.1f)
        assertEquals("idle_angry", runtime.currentName(0))
        p.run(30f)
        assertEquals("idle_angry", runtime.currentName(0))
    }

    @Test
    fun `a long idle plays once, then the idle returns`() {
        val p = started()
        assertTrue(p.triggerLongIdle())
        assertEquals(
            listOf(
                "set 0 long_idle_default loop=false mix=${mix.intoLongIdle}",
                "add 0 idle_default loop=true mix=${mix.longIdleToIdle}",
            ),
            runtime.calls,
        )
        assertFalse("not stacked on itself", p.triggerLongIdle())
        p.run(contract.durations.getValue("long_idle_default") + 0.2f)
        assertEquals("idle_default", runtime.currentName(0))
        assertTrue("available again once back in idle", p.triggerLongIdle())
    }

    @Test
    fun `a long idle belongs to the state shown`() {
        val p = started()
        p.show(Expression.SLEEPY)
        p.run(contract.durations.getValue("to_sleepy") + 0.1f)
        runtime.calls.clear()
        assertTrue(p.triggerLongIdle())
        assertEquals("set 0 long_idle_sleepy loop=false mix=${mix.intoLongIdle}", runtime.calls.first())
    }

    @Test
    fun `a long idle is refused during a transition or while paused`() {
        val p = started()
        p.show(Expression.HAPPY)
        assertFalse(p.triggerLongIdle())
        p.run(2f)
        p.pause()
        assertFalse(p.triggerLongIdle())
    }

    @Test
    fun `a state change interrupts a long idle`() {
        val p = started()
        p.triggerLongIdle()
        p.run(1f)
        runtime.calls.clear()
        p.show(Expression.SURPRISED)
        assertEquals("set 0 to_surprised loop=false mix=${mix.interrupt}", runtime.calls.first())
    }

    @Test
    fun `return to idle interrupts whatever plays`() {
        val p = started()
        p.triggerLongIdle()
        runtime.calls.clear()
        p.returnToIdle()
        assertEquals(listOf("set 0 idle_default loop=true mix=${mix.interrupt}"), runtime.calls)
        runtime.calls.clear()
        p.returnToIdle()
        assertEquals("already idle: nothing to do", emptyList<String>(), runtime.calls)
    }

    @Test
    fun `a stranded one-shot never leaves the character stuck`() {
        val p = started()
        runtime.strand(0, "to_happy")
        runtime.emitComplete(0, "to_happy")
        p.advance(FRAME)
        assertEquals("idle_default", runtime.currentName(0))
    }

    @Test
    fun `the entrance clears its track when it completes and the exit hides`() {
        player.start(Expression.DEFAULT, entrance = true)
        assertFalse(player.hidden)
        player.run(contract.durations.getValue("enter") + 0.1f)
        assertTrue(runtime.calls.contains("clear 1"))
        assertEquals(-1, runtime.current(1))
        player.exit()
        assertEquals("set 1 exit loop=false mix=0.0", runtime.calls.last())
        assertFalse(player.hidden)
        player.run(contract.durations.getValue("exit") + 0.1f)
        assertTrue(player.hidden)
        assertEquals("the exit holds its last frame", "exit", runtime.currentName(1))
        player.enter()
        assertFalse(player.hidden)
    }

    @Test
    fun `pause stops time and resume does not jump`() {
        val p = started()
        p.pause()
        assertTrue(p.paused)
        repeat(10) { p.advance(FRAME) }
        assertEquals(emptyList<Float>(), runtime.updates)
        p.resume()
        p.advance(5f)
        p.advance(FRAME)
        assertEquals(listOf(0f, FRAME), runtime.updates)
    }

    @Test
    fun `state changes made while paused apply at once and play on resume`() {
        val p = started()
        p.pause()
        p.show(Expression.SAD)
        assertEquals("to_sad", runtime.currentName(0))
        assertEquals(Expression.SAD, p.expression)
    }

    @Test
    fun `release closes the runtime once and makes every later call a no-op`() {
        val p = started()
        p.close()
        p.close()
        assertEquals(1, runtime.closed)
        assertTrue(p.released)
        p.show(Expression.HAPPY)
        p.triggerLongIdle()
        p.enter()
        p.exit()
        p.advance(FRAME)
        assertFalse(p.build(RigGeometry(runtime.info)))
        assertEquals(emptyList<String>(), runtime.calls)
        assertEquals(emptyList<Float>(), runtime.updates)
    }

    @Test
    fun `reduced motion swaps straight to the idle and holds it still`() {
        val p = RigCharacterPlayer.create(runtime, contract, reducedMotion = true)!!
        p.start(Expression.DEFAULT, entrance = true)
        assertEquals(listOf("set 0 idle_default loop=true mix=0.0"), runtime.calls)
        runtime.calls.clear()
        p.show(Expression.ANGRY)
        assertEquals(listOf("set 0 idle_angry loop=true mix=0.0"), runtime.calls)
        assertFalse(p.triggerLongIdle())
        p.advance(FRAME)
        assertEquals(listOf(0f), runtime.updates)
    }

    @Test
    fun `a rig missing a contract animation is refused`() {
        val partial = FakeRigRuntime(contract.durations - "long_idle_sad")
        assertNull(RigCharacterPlayer.create(partial, contract))
    }

    private fun FakeRigRuntime.currentName(track: Int): String? =
        contract.durations.keys.toList().getOrNull(
            current(track),
        )

    private companion object {
        const val FRAME = 1f / 60f
    }
}
