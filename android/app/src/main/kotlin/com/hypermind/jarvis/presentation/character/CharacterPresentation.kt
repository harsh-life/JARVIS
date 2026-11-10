package com.hypermind.jarvis.presentation.character

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * The character's runtime (OD-CHAR-4): the server's activity reports and local activity go into the
 * [CharacterController], whose decision is [presented] — the expression and how strongly to present
 * it. [expression] is the same decision without the intensity, for screens that only need the state.
 *
 * - The expression presents JARVIS's operational activity as the server's activity scorer computed
 *   it — not the user, not a feeling. It is never the account of what JARVIS is doing (the status
 *   line is) and nothing that authorizes, confirms or executes reads it.
 * - Everything is in memory only: no report, score or expression is persisted, logged or sent. The
 *   raw report text is parsed and dropped; only the latest scores are kept, until they go stale.
 * - It reads no utterance, request text or audio: what the user says plays no part (OD-CHAR-4
 *   retired the on-device cue scorer).
 *
 * Confined to [scope]'s dispatcher: call it from that thread (the main thread in the app).
 */
class CharacterPresentation(
    private val scope: CoroutineScope,
    /** Monotonic milliseconds (the app passes `SystemClock.elapsedRealtime`). */
    private val clock: () -> Long,
    val profile: CharacterProfile = CharacterProfile.BUILT_IN,
    val settings: CharacterSettings = CharacterSettings(),
) {
    private val controller = CharacterController(settings, clock())
    private val current = MutableStateFlow(controller.presented)
    private val currentExpression = MutableStateFlow(controller.shown)
    private val wake = Channel<Unit>(Channel.CONFLATED)

    /** The committed expression and intensity. Equal values are not re-emitted. */
    val presented: StateFlow<PresentedCharacter> = current.asStateFlow()

    /** [presented]'s expression alone. */
    val expression: StateFlow<Expression> = currentExpression.asStateFlow()

    /** How often the app should ask the server for a fresh report while it is in front. */
    val pollIntervalMillis: Long get() = settings.activityPollMillis

    init {
        scope.launch { drive() }
    }

    /** The user did something here: wakes a sleepy character and restarts the local idle timer. */
    fun onActivity() {
        controller.onActivity(clock())
        changed()
    }

    /**
     * The server's activity report, as the raw response body. Validated whole; anything invalid
     * (or null — no answer) is discarded and the last valid report stands until it goes stale.
     */
    fun onActivityReport(raw: String?) {
        controller.onReport(ActivityReport.parse(raw), clock())
        changed()
    }

    /** Development preview: show [expression] at [intensity] (neutral unless chosen) until [reset]. */
    fun preview(
        expression: Expression,
        intensity: Int = IntensityPolicy.NEUTRAL,
    ) {
        controller.pin(expression, clock(), intensity)
        changed()
    }

    fun reset() {
        controller.unpin(clock())
        changed()
    }

    private fun changed() {
        publish()
        wake.trySend(Unit)
    }

    private fun publish() {
        current.value = controller.presented
        currentExpression.value = controller.shown
    }

    private suspend fun drive() {
        while (true) {
            val at = controller.nextChangeAt(clock())
            if (at == null) {
                wake.receive()
            } else {
                val wait = at - clock()
                if (wait > 0) withTimeoutOrNull(wait) { wake.receive() }
            }
            controller.tick(clock())
            publish()
        }
    }
}
