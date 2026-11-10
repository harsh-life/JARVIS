package com.hypermind.jarvis

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * OD-CHAR-4: the one place the server's activity report reaches the character. It asks for the
 * report — a read-only, owner-scoped `GET /presentation/activity` — every poll interval while it
 * runs (the app runs it only while the screen is in front), and once more soon after anything that
 * changes JARVIS's work (a task snapshot changed), and hands the raw answer on. It never acts on a
 * task, never reads one, and keeps nothing: the character package validates the text and drops it.
 */
object CharacterActivityFeed {
    /** Nudges closer together than this share one request. */
    const val MIN_GAP_MILLIS = 2_000L

    /**
     * Runs until cancelled: [fetch] now, then after [intervalMillis], or sooner when [nudges] emits
     * (a nudge within [MIN_GAP_MILLIS] of the last request waits out the gap). Every answer — null
     * when the request failed — goes to [onReport]. [clock] is monotonic milliseconds. [nudges] should
     * not replay a current value (pass a StateFlow with `drop(1)`).
     */
    @Suppress("TooGenericExceptionCaught")
    suspend fun run(
        intervalMillis: Long,
        nudges: Flow<*>,
        clock: () -> Long,
        fetch: suspend () -> String?,
        onReport: (String?) -> Unit,
    ) = coroutineScope {
        val nudged = Channel<Unit>(Channel.CONFLATED)
        launch { nudges.collect { nudged.trySend(Unit) } }
        while (true) {
            val asked = clock()
            val raw =
                try {
                    fetch()
                } catch (e: CancellationException) {
                    throw e
                } catch (ignored: Exception) {
                    null
                }
            onReport(raw)
            withTimeoutOrNull(intervalMillis) {
                nudged.receive()
                val early = MIN_GAP_MILLIS - (clock() - asked)
                if (early > 0) delay(early)
            }
        }
    }
}
