package com.hypermind.jarvis.contract

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.util.Base64
import kotlin.random.Random

class Base64UrlTest {
    @Test
    fun `matches the JDK encoder for every length`() {
        val random = Random(7)
        for (size in 0..200) {
            val data = random.nextBytes(size)
            val expected = Base64.getUrlEncoder().withoutPadding().encodeToString(data)
            assertEquals(expected, Base64Url.encode(data))
            assertArrayEquals(data, Base64Url.decode(expected))
        }
    }

    @Test
    fun `rejects what is not base64url`() {
        assertThrows(IllegalArgumentException::class.java) { Base64Url.decode("ab+/") }
        assertThrows(IllegalArgumentException::class.java) { Base64Url.decode("a") }
        assertThrows(IllegalArgumentException::class.java) { Base64Url.decode("abéc") }
    }
}
