package com.hypermind.jarvis.presentation.character

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.time.Instant
import kotlin.coroutines.CoroutineContext

/**
 * The character's runtime (OD-CHAR-1/2): the current interaction and verified
 * events go to the [provider], its result through [StateSelector], and the
 * selection into the [CharacterController], whose decision is [expression].
 *
 * - Everything is in memory only: no utterance, score or expression is
 *   persisted, logged or sent anywhere, and nothing here builds a profile of
 *   the user. [MAX_CONTEXT] recent utterances of this session are kept for a
 *   provider that needs them to interpret the current one.
 * - It never reads task, connection or error state and never acts: it is
 *   told about activity, utterances and verified events, and shows a face.
 * - Of overlapping inferences only the latest is applied; a provider that
 *   fails or throws counts as "no result" (the baseline stays).
 *
 * Confined to [scope]'s dispatcher: call it from that thread (the main
 * thread in the app).
 */
class CharacterPresentation(
    private val scope: CoroutineScope,
    /** Monotonic milliseconds (the app passes `SystemClock.elapsedRealtime`). */
    private val clock: () -> Long,
    val profile: CharacterProfile = CharacterProfile.BUILT_IN,
    val settings: CharacterSettings = CharacterSettings(),
    private val provider: PresentationInferenceProvider = LocalCueScorer(),
    private val wallClock: () -> Instant = Instant::now,
    private val inferenceContext: CoroutineContext = Dispatchers.Default,
) {
    private val controller = CharacterController(settings, clock())
    private val current = MutableStateFlow(controller.shown)
    private val wake = Channel<Unit>(Channel.CONFLATED)
    private val recent = ArrayDeque<String>()
    private var generation = 0L
    private var inference: Job? = null

    val expression: StateFlow<Expression> = current.asStateFlow()

    init {
        scope.launch { drive() }
    }

    /** The user did something meaningful: wakes a sleepy character and restarts the idle timer. */
    fun onActivity() {
        controller.onActivity(clock())
        changed()
    }

    /** The user's current request (typed, or [spoken] and transcribed). */
    fun onUtterance(
        text: String,
        spoken: Boolean = false,
    ) {
        onActivity()
        val utterance = text.take(MAX_UTTERANCE)
        infer(InferenceInput(utterance = utterance, spoken = spoken, context = recent.toList()))
        recent.addLast(utterance)
        while (recent.size > MAX_CONTEXT) recent.removeFirst()
    }

    /** A verified application event. */
    fun onEvent(event: PresentationEvent) = infer(InferenceInput(context = recent.toList(), events = listOf(event)))

    /** Development preview: show [expression] until [reset]. */
    fun preview(expression: Expression) {
        controller.pin(expression, clock())
        changed()
    }

    fun reset() {
        controller.unpin(clock())
        changed()
    }

    @Suppress("TooGenericExceptionCaught")
    private fun infer(input: InferenceInput) {
        val mine = ++generation
        inference?.cancel()
        inference =
            scope.launch {
                val result =
                    try {
                        withContext(inferenceContext) { provider.infer(input) }
                    } catch (e: CancellationException) {
                        throw e
                    } catch (ignored: Exception) {
                        null
                    }
                if (mine != generation) return@launch
                controller.onSelection(StateSelector.select(result, settings, wallClock()), clock())
                changed()
            }
    }

    private fun changed() {
        current.value = controller.shown
        wake.trySend(Unit)
    }

    private suspend fun drive() {
        while (true) {
            val at = controller.nextChangeAt()
            if (at == null) {
                wake.receive()
            } else {
                val wait = at - clock()
                if (wait > 0) withTimeoutOrNull(wait) { wake.receive() }
            }
            controller.tick(clock())
            current.value = controller.shown
        }
    }

    companion object {
        const val MAX_CONTEXT = 3
        const val MAX_UTTERANCE = 2_000
    }
}
