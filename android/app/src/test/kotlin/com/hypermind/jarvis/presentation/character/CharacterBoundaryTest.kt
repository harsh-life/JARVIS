package com.hypermind.jarvis.presentation.character

import com.hypermind.jarvis.presentation.PresentationSignal
import com.hypermind.jarvis.presentation.PresentationState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * OD-CHAR-1/4, checked on the source: the character's expression is
 * presentation only. It presents the server's activity report (OD-CHAR-4) and
 * nothing else — the character package cannot see tasks, connection or error
 * state, the app's operational state never carries an expression, the user's
 * words never reach it, and nothing that authorizes, confirms, executes,
 * perceives, holds a credential or talks to the server can read it.
 */
class CharacterBoundaryTest {
    private val root = File("src/main/kotlin/com/hypermind/jarvis")

    private val characterSources =
        root
            .resolve("presentation/character")
            .walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .toList() +
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
    fun `only the screen, the application and the activity feed hold the character`() {
        val allowed =
            characterSources.toSet() + root.resolve("MainActivity.kt") + root.resolve("JarvisApplication.kt") +
                root.resolve("CharacterActivityFeed.kt")
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
    fun `the activity feed only asks - it reads no task and never acts on one`() {
        val feed = root.resolve("CharacterActivityFeed.kt").readText()
        for (action in listOf(".submit(", ".approve(", ".decline(", ".cancel(", ".retry(", ".dismiss(", ".runAgent(")) {
            assertTrue(action, !feed.contains(action))
        }
        val imports = imports(root.resolve("CharacterActivityFeed.kt")).filter { it.startsWith("com.hypermind.jarvis") }
        assertEquals(emptyList<String>(), imports)
        // The one request it makes, wired in MainActivity, is the read-only activity report.
        val screen = root.resolve("MainActivity.kt").readText()
        assertTrue(screen.contains("CharacterActivityFeed.run("))
        assertTrue(screen.contains(".activity.report("))
    }

    @Test
    fun `the user's words never reach the character`() {
        // The only text the character accepts is the server's report; there is no utterance,
        // transcript or request input any more (OD-CHAR-4 retired the on-device cue scorer).
        val textInputs =
            CharacterPresentation::class.java.declaredMethods
                .filter {
                    java.lang.reflect.Modifier
                        .isPublic(it.modifiers)
                }.filter { m -> m.parameterTypes.any { it == String::class.java } }
                .map { it.name }
        assertEquals(listOf("onActivityReport"), textInputs)
        val screen = root.resolve("MainActivity.kt").readText()
        assertTrue(!screen.contains("onUtterance"))
    }

    @Test
    fun `the task, confirmation and authorization paths never reference the character`() {
        for (dir in listOf("tasks", "auth", "action", "channel", "permissions", "privileged", "perception", "push")) {
            for (file in root.resolve(dir).walkTopDown().filter { it.isFile && it.extension == "kt" }) {
                val text = file.readText()
                assertTrue(file.path, !text.contains("character", ignoreCase = true) && !text.contains("Expression."))
            }
        }
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
