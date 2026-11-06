package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

/** The bundled control contract, and what an invalid one does (it is refused whole). */
class RigContractTest {
    private val text = RigAssetsFixture.text(RigAssets.CONTRACT)
    private val contract = RigAssetsFixture.contract()

    @Test
    fun `the bundled files are the canonical export, byte for byte, and the profile is pinned too`() {
        val pins =
            RigAssetsFixture
                .text("SHA256SUMS")
                .lines()
                .filter { it.isNotBlank() }
                .associate { line -> line.substringAfter("  ") to line.substringBefore("  ") }
        // The rig project's four exports, unchanged, and JARVIS's own intensity profile beside them.
        assertEquals(
            setOf(RigAssets.SKELETON, RigAssets.ATLAS, RigAssets.PAGE, RigAssets.CONTRACT, RigAssets.INTENSITY),
            pins.keys,
        )
        for ((name, sha) in pins) {
            val digest = MessageDigest.getInstance("SHA-256").digest(RigAssetsFixture.bytes(name))
            assertEquals(name, sha, digest.joinToString("") { "%02x".format(it) })
        }
    }

    @Test
    fun `all seven states have an idle, a long idle and an entry`() {
        for (e in Expression.entries) {
            assertEquals("idle_${e.wire}", contract.idle.getValue(e))
            assertEquals("long_idle_${e.wire}", contract.longIdle.getValue(e))
            assertEquals("to_${e.wire}", contract.entry.getValue(e))
        }
        assertEquals("enter", contract.enter)
        assertEquals("exit", contract.exit)
    }

    @Test
    fun `the five pair transitions and the rule for every other pair`() {
        assertEquals(
            mapOf(
                (Expression.HAPPY to Expression.SATISFIED) to "trans_happy_to_satisfied",
                (Expression.SURPRISED to Expression.DEFAULT) to "trans_surprised_to_default",
                (Expression.ANGRY to Expression.SATISFIED) to "trans_angry_to_satisfied",
                (Expression.SAD to Expression.HAPPY) to "trans_sad_to_happy",
                (Expression.SLEEPY to Expression.DEFAULT) to "trans_sleepy_to_default",
            ),
            contract.pairs,
        )
        assertEquals("trans_sad_to_happy", contract.transition(Expression.SAD, Expression.HAPPY))
        assertEquals("to_happy", contract.transition(Expression.DEFAULT, Expression.HAPPY))
        assertEquals("to_angry", contract.transition(Expression.SAD, Expression.ANGRY))
    }

    @Test
    fun `mix times are the contract's`() {
        with(contract.mix) {
            assertEquals(0.3f, intoEntry)
            assertEquals(0.15f, intoPair)
            assertEquals(0.2f, toIdle)
            assertEquals(0.3f, intoLongIdle)
            assertEquals(0.4f, longIdleToIdle)
            assertEquals(0.15f, interrupt)
        }
    }

    @Test
    fun `28 animations, every named one among them, with durations`() {
        assertEquals(28, contract.durations.size)
        assertTrue(contract.durations.values.all { it > 0f })
        assertTrue(contract.durations.keys.containsAll(contract.animationNames))
        assertEquals(28, contract.animationNames.size)
    }

    @Test
    fun `an invalid contract is refused whole`() {
        assertNull(RigContract.parse(""))
        assertNull(RigContract.parse("{"))
        assertNull(RigContract.parse("[]"))
        assertNull(
            RigContract.parse(text.replace("jarvis-character-rig-contract/1", "jarvis-character-rig-contract/2")),
        )
        assertNull(
            "a missing state",
            RigContract.parse(text.replace("\"sleepy\": \"idle_sleepy\"", "\"sleepier\": \"idle_sleepy\"")),
        )
        assertNull("an unknown state", RigContract.parse(text.replace("\"happy>satisfied\"", "\"bored>satisfied\"")))
        assertNull("a mix out of range", RigContract.parse(text.replace("\"interrupt\": 0.15", "\"interrupt\": 5")))
        assertNull("an empty name", RigContract.parse(text.replace("\"enter\": \"enter\"", "\"enter\": \"\"")))
        assertNull(
            "a named animation with no duration",
            RigContract.parse(text.replace("\"to_sad\": {", "\"to_sadder\": {")),
        )
        assertNotNull(RigContract.parse(text))
    }
}
