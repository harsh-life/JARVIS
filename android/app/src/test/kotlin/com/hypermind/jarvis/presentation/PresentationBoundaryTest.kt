package com.hypermind.jarvis.presentation

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * docs/23 §7 / Phase G boundary, checked on the source: the presentation layer
 * (and the overlay and the future-character indicator) can only *show* state.
 * It cannot reach anything that authorizes, executes, perceives, holds a
 * credential or persists screen data — so replacing the indicator with the
 * character later cannot widen authority, and no UI path bypasses the server.
 */
class PresentationBoundaryTest {
    private val root = File("src/main/kotlin/com/hypermind/jarvis")

    private fun sources(dir: String): List<File> =
        root
            .resolve(dir)
            .walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .toList()
            .also { assertTrue("no sources under $dir", it.isNotEmpty()) }

    private fun imports(file: File): List<String> =
        file.readLines().filter { it.startsWith("import ") }.map { it.removePrefix("import ").trim() }

    /** Anything that authorizes, executes, perceives the screen, holds keys or talks to the server. */
    private val privileged =
        listOf(
            "com.hypermind.jarvis.privileged",
            "com.hypermind.jarvis.action",
            "com.hypermind.jarvis.permissions",
            "com.hypermind.jarvis.push",
            "com.hypermind.jarvis.perception",
            "com.hypermind.jarvis.auth.ApiClient",
            "com.hypermind.jarvis.auth.SessionManager",
            "com.hypermind.jarvis.auth.StepUpFlow",
            "com.hypermind.jarvis.auth.DeviceKey",
            "com.hypermind.jarvis.auth.KeystoreDeviceKeyStore",
            "com.hypermind.jarvis.auth.StepUpKey",
            "com.hypermind.jarvis.tasks.TaskController",
            "com.hypermind.jarvis.channel.DeviceChannel",
            "com.hypermind.jarvis.channel.OperationHandler",
            "com.hypermind.jarvis.contract.DeviceGuard",
        )

    @Test
    fun `presentation and overlay code cannot reach authority, execution, perception or credentials`() {
        for (file in sources("presentation") + sources("overlay")) {
            val bad = imports(file).filter { imp -> privileged.any { imp.startsWith(it) } }
            assertTrue("${file.name} imports $bad", bad.isEmpty())
        }
    }

    @Test
    fun `the future-character indicator reads signals only`() {
        val indicator = sources("presentation/ui")
        for (file in indicator) {
            val app = imports(file).filter { it.startsWith("com.hypermind.jarvis") }
            val allowed =
                app.all {
                    it.startsWith("com.hypermind.jarvis.presentation.PresentationSignal") ||
                        it.startsWith("com.hypermind.jarvis.ui.theme")
                }
            assertTrue("${file.name} imports $app", allowed)
        }
    }

    @Test
    fun `the overlay offers no approval and cannot submit a task`() {
        for (file in sources("overlay")) {
            val text = file.readText()
            assertTrue(
                file.name,
                !text.contains("approve(") && !text.contains(".submit(") && !text.contains("confirmTask"),
            )
        }
    }

    @Test
    fun `no presentation, overlay or task-state code logs anything`() {
        for (file in sources("presentation") + sources("overlay") + sources("tasks") + sources("ui")) {
            val text = file.readText()
            assertTrue(file.name, !text.contains("android.util.Log") && !text.contains("println("))
        }
    }

    @Test
    fun `nothing in the presentation layer persists state beyond the overlay switch and the task id`() {
        val persisting =
            (sources("presentation") + sources("overlay") + sources("tasks") + sources("ui"))
                .filter { it.readText().contains("SharedPreferences") }
                .map { it.name }
                .toSet()
        // OverlaySettings (a boolean) and TaskMemory (a task id, never content).
        assertTrue("$persisting", persisting == setOf("Overlay.kt", "TaskTracker.kt"))
    }
}
