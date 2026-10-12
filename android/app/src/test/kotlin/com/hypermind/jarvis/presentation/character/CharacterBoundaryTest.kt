package com.hypermind.jarvis.presentation.character

import com.hypermind.jarvis.presentation.PresentationSignal
import com.hypermind.jarvis.presentation.PresentationState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * OD-CHAR-1, checked on the source: the character's expression is
 * presentation only. It is never derived from operational state (task,
 * connection, error), operational state never carries it, and nothing that
 * authorizes, confirms, executes, perceives, holds a credential or talks to
 * the server can read it.
 */
class CharacterBoundaryTest {
    private val root = File("src/main/kotlin/com/hypermind/jarvis")

    private val characterSources =
        root.resolve("presentation/character").walkTopDown().filter { it.isFile && it.extension == "kt" }.toList() +
            root.resolve("presentation/ui/Character.kt")

    private fun imports(file: File): List<String> =
        file.readLines().filter { it.startsWith("import ") }.map { it.removePrefix("import ").trim() }

    @Test
    fun `character code reads nothing but its own package`() {
        assertTrue(characterSources.size >= 4)
        for (file in characterSources) {
            val app = imports(file).filter { it.startsWith("com.hypermind.jarvis") }
            val bad = app.filterNot { it.startsWith("com.hypermind.jarvis.presentation.character") }
            assertTrue("${file.name} imports $bad", bad.isEmpty())
        }
    }

    @Test
    fun `only the screen and the application hold the character`() {
        val allowed = characterSources.toSet() + root.resolve("MainActivity.kt") + root.resolve("JarvisApplication.kt")
        val readers =
            root
                .walkTopDown()
                .filter { it.isFile && it.extension == "kt" && it !in allowed }
                .filter { file ->
                    val text = file.readText()
                    text.contains("presentation.character") ||
                        text.contains("CharacterPresentation") ||
                        Regex("""\bCharacter\(""").containsMatchIn(text)
                }.map { it.path }
                .toList()
        assertEquals(emptyList<String>(), readers)
    }

    @Test
    fun `operational state carries no expression`() {
        for (type in listOf(PresentationState::class.java, PresentationSignal::class.java)) {
            val fields = type.declaredFields.map { it.type }
            assertTrue("${type.simpleName}", Expression::class.java !in fields)
        }
    }

    @Test
    fun `the status, task and error mapping never mentions the character`() {
        val operational = root.resolve("presentation").listFiles { f -> f.isFile && f.extension == "kt" }!!.toList()
        assertTrue(operational.any { it.name == "Presenter.kt" })
        for (file in operational + root.resolve("presentation/ui/StatusIndicator.kt")) {
            val text = file.readText()
            val reads = text.contains("presentation.character") || Regex("""\bExpression\b""").containsMatchIn(text)
            assertTrue(file.name, !reads)
        }
    }

    @Test
    fun `the character is never persisted or sent`() {
        for (file in characterSources) {
            val text = file.readText()
            for (word in listOf("SharedPreferences", "okhttp", "ApiClient", "DeviceChannel", "android.util.Log")) {
                assertTrue("${file.name} mentions $word", !text.contains(word))
            }
        }
    }
}
