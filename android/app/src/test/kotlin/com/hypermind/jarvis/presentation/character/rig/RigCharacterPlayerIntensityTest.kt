package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The player's side of presentation intensity: it turns the (expression, intensity) it is given
 * into the profile's two gains and tells the runtime only when they change. An intensity alone
 * never restarts an animation; reduced motion and a missing profile never touch the layers.
 */
class RigCharacterPlayerIntensityTest {
    private val contract = RigAssetsFixture.contract()
    private val profile = requireNotNull(RigIntensityProfile.parse(RigAssetsFixture.text(RigAssets.INTENSITY)))
    private val runtime = FakeRigRuntime.forContract(contract)
    private val player = RigCharacterPlayer.create(runtime, contract, profile = profile)!!

    private fun shape(
        state: Expression,
        intensity: Int,
    ): String {
        val g = profile.gains(state, intensity)
        return "shape ${contract.idle.getValue(state)} posture=${g.posture} motion=${g.motion}"
    }

    private fun shapeCalls() = runtime.calls.filter { it.startsWith("shape") }

    @Test
    fun `a neutral start says nothing to the layers, a strong one sends its gains once`() {
        player.start(Expression.DEFAULT, 5, entrance = true)
        assertEquals(emptyList<String>(), shapeCalls())
        val other = FakeRigRuntime.forContract(contract)
        RigCharacterPlayer.create(other, contract, profile = profile)!!.start(Expression.HAPPY, 9, entrance = false)
        val g = profile.gains(Expression.HAPPY, 9)
        assertEquals(
            listOf("shape idle_happy posture=${g.posture} motion=${g.motion}"),
            other.calls.filter {
                it.startsWith("shape")
            },
        )
    }

    @Test
    fun `a new intensity for the shown expression only retargets the gains`() {
        player.start(Expression.SAD, 5, entrance = false)
        runtime.calls.clear()
        player.show(Expression.SAD, 9)
        assertEquals("no animation restarts", listOf(shape(Expression.SAD, 9)), runtime.calls)
        assertEquals(9, player.intensity)
        runtime.calls.clear()
        player.show(Expression.SAD, 9)
        assertEquals("the same pair again is a no-op", emptyList<String>(), runtime.calls)
        player.show(Expression.SAD, 2)
        assertEquals(listOf(shape(Expression.SAD, 2)), runtime.calls)
    }

    @Test
    fun `a dead-band step for the shown expression reaches the layers`() {
        // The controller moved happy from 4 (score 7) to 6 (score 8): the player retargets.
        player.start(Expression.HAPPY, 4, entrance = false)
        runtime.calls.clear()
        player.show(Expression.HAPPY, 6)
        assertEquals(listOf(shape(Expression.HAPPY, 6)), runtime.calls)
    }

    @Test
    fun `intensities with the same gains send nothing`() {
        player.start(Expression.ANGRY, 7, entrance = false)
        player.show(Expression.ANGRY, 5)
        runtime.calls.clear()
        player.show(Expression.ANGRY, 6) // 5 and 6 are both the canonical animation
        assertEquals(emptyList<String>(), runtime.calls)
    }

    @Test
    fun `a new expression plays its transition and sends its own gains`() {
        player.start(Expression.DEFAULT, 5, entrance = false)
        runtime.calls.clear()
        player.show(Expression.HAPPY, 8)
        assertEquals(
            listOf(
                shape(Expression.HAPPY, 8),
                "set 0 to_happy loop=false mix=${contract.mix.intoEntry}",
                "add 0 idle_happy loop=true mix=${contract.mix.toIdle}",
            ),
            runtime.calls,
        )
        runtime.calls.clear()
        player.show(Expression.DEFAULT, 5) // back to canonical: the happy gains ramp out
        assertTrue(runtime.calls.first(), runtime.calls.first().startsWith("shape idle_default posture=1.0 motion=1.0"))
    }

    @Test
    fun `the exit fades the layers out and the entrance brings them back`() {
        player.start(Expression.SLEEPY, 2, entrance = false)
        runtime.calls.clear()
        player.exit()
        assertEquals(listOf("set 1 exit loop=false mix=0.0", "shape none posture=1.0 motion=1.0"), runtime.calls)
        runtime.calls.clear()
        player.enter()
        assertEquals(listOf(shape(Expression.SLEEPY, 2), "set 1 enter loop=false mix=0.0"), runtime.calls)
    }

    @Test
    fun `reduced motion ignores intensity entirely`() {
        val still = FakeRigRuntime.forContract(contract)
        val p = RigCharacterPlayer.create(still, contract, reducedMotion = true, profile = profile)!!
        p.start(Expression.HAPPY, 10, entrance = true)
        p.show(Expression.HAPPY, 1)
        p.show(Expression.SAD, 9)
        p.exit()
        p.enter()
        assertEquals(emptyList<String>(), still.calls.filter { it.startsWith("shape") })
    }

    @Test
    fun `without a profile every intensity plays canonically`() {
        val plain = FakeRigRuntime.forContract(contract)
        val p = RigCharacterPlayer.create(plain, contract)!!
        p.start(Expression.HAPPY, 10, entrance = false)
        p.show(Expression.SAD, 1)
        assertEquals(emptyList<String>(), plain.calls.filter { it.startsWith("shape") })
        assertEquals(1, p.intensity)
    }

    @Test
    fun `pause, resume and release are unchanged, and nothing reaches a released runtime`() {
        player.start(Expression.SAD, 9, entrance = false)
        player.pause()
        player.show(Expression.SAD, 3) // applies at once, plays on resume
        player.resume()
        player.close()
        runtime.calls.clear()
        player.show(Expression.HAPPY, 10)
        player.exit()
        assertEquals(emptyList<String>(), runtime.calls)
        assertEquals(1, runtime.closed)
    }

    @Test
    fun `an intensity out of range is a programming error`() {
        player.start(Expression.DEFAULT, 5, entrance = false)
        assertThrows(IllegalArgumentException::class.java) { player.show(Expression.SAD, 0) }
        assertThrows(IllegalArgumentException::class.java) { player.show(Expression.SAD, 11) }
    }
}
