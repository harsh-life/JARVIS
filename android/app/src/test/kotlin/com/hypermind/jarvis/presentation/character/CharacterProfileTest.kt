package com.hypermind.jarvis.presentation.character

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.File

/** OD-CHAR-1/2: the states, the built-in profile, fallback and sizing — pure, no Android. */
class CharacterProfileTest {
    private val characterDir = File(System.getProperty("jarvis.character.dir") ?: "../../assets/character")

    private val expected =
        mapOf(
            Expression.HAPPY to "happy.webp",
            Expression.SATISFIED to "satisfied.png",
            Expression.DEFAULT to "default.png",
            Expression.SLEEPY to "sleepy.webp",
            Expression.SURPRISED to "surprised.webp",
            Expression.ANGRY to "angry.webp",
            Expression.SAD to "sad.webp",
        )

    @Test
    fun `the seven states are in the owner's order, as in the server's contract`() {
        assertEquals(
            listOf("happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad"),
            Expression.entries.map { it.wire },
        )
    }

    @Test
    fun `the built-in profile maps every state to its image and falls back to default`() {
        val profile = CharacterProfile.BUILT_IN
        assertEquals(Expression.DEFAULT, profile.fallback)
        assertEquals(expected, profile.states)
        assertEquals(Expression.entries, profile.states.keys.toList())
        expected.forEach { (state, asset) -> assertEquals(asset, profile.assetFor(state)) }
        assertEquals("character/default/happy.webp", profile.assetPath(profile.assetFor(Expression.HAPPY)))
    }

    @Test
    fun `the bundled profile file is the built-in profile`() {
        val text = characterDir.resolve("default/profile.json").readText()
        assertEquals(CharacterProfile.BUILT_IN, CharacterProfile.parse(text))
    }

    @Test
    fun `the replaced images are mapped by their real extensions`() {
        assertEquals("satisfied.png", CharacterProfile.BUILT_IN.assetFor(Expression.SATISFIED))
        assertEquals("surprised.webp", CharacterProfile.BUILT_IN.assetFor(Expression.SURPRISED))
        assertEquals(false, characterDir.resolve("default/satisfied.webp").exists())
    }

    @Test
    fun `anything unknown parses to default`() {
        for (bad in listOf("furious", "", "HAPPY", " happy", "Default", null)) {
            assertEquals("$bad", Expression.DEFAULT, Expression.parse(bad))
        }
        for (state in Expression.entries) assertEquals(state, Expression.parse(state.wire))
    }

    @Test
    fun `a state the profile omits shows its fallback`() {
        val partial =
            CharacterProfile.parse(
                """{"profile_id": "partial", "states": {"default": "default.png", "happy": "happy.webp"}}""",
            )
        assertNotNull(partial)
        assertEquals("happy.webp", partial!!.assetFor(Expression.HAPPY))
        assertEquals("default.png", partial.assetFor(Expression.SAD))
    }

    @Test
    fun `a malformed profile is rejected whole`() {
        val bad =
            listOf(
                "",
                "[]",
                "not json",
                """{"profile_id": "x", "states": {"happy": "happy.webp"}}""",
                """{"profile_id": "x", "states": {"default": "../secrets.png"}}""",
                """{"profile_id": "x", "states": {"default": "sub/default.png"}}""",
                """{"profile_id": "x", "states": {"default": "default.svg"}}""",
                """{"profile_id": "../x", "states": {"default": "default.png"}}""",
                """{"profile_id": "x", "states": {"furious": "default.png", "default": "default.png"}}""",
                """{"profile_id": "x", "states": {}}""",
                """{"profile_id": "x", "states": {"default": 3}}""",
                """{"profile_id": "x", "schema_version": 2, "states": {"default": "default.png"}}""",
                """{"profile_id": "x", "states": {"default": "default.png"}, "mood": "happy"}""",
                """{"profile_id": "x", "fallback": "furious", "states": {"default": "default.png"}}""",
                """{"states": {"default": "default.png"}}""",
            )
        for (text in bad) assertNull(text, CharacterProfile.parse(text))
    }

    @Test
    fun `images are decoded no larger than the slot needs, and never upscaled`() {
        assertEquals(1, CharacterAssets.sampleSize(heightPx = 585, targetHeightPx = 600))
        assertEquals(1, CharacterAssets.sampleSize(heightPx = 1536, targetHeightPx = 1000))
        assertEquals(2, CharacterAssets.sampleSize(heightPx = 1536, targetHeightPx = 600))
        assertEquals(4, CharacterAssets.sampleSize(heightPx = 1536, targetHeightPx = 300))
        assertEquals(1, CharacterAssets.sampleSize(heightPx = 0, targetHeightPx = 300))
        assertEquals(1, CharacterAssets.sampleSize(heightPx = 1536, targetHeightPx = 0))
    }

    @Test
    fun `the character scales with the window, within bounds`() {
        // Small phone, typical phone, landscape phone, tablet, tiny window.
        assertEquals(134.4f, CharacterSizing.heightDp(320, 568), 0.01f)
        assertEquals(172.62f, CharacterSizing.heightDp(411, 891), 0.01f)
        assertEquals(98.64f, CharacterSizing.heightDp(891, 411), 0.01f)
        assertEquals(CharacterSizing.MAX_DP, CharacterSizing.heightDp(800, 1280), 0.01f)
        assertEquals(CharacterSizing.MIN_DP, CharacterSizing.heightDp(240, 320), 0.01f)
    }
}
