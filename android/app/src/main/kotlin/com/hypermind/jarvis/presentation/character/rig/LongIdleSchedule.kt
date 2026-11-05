package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression

/**
 * When the shown state's long-idle action is due: after [periodMillis] (ten minutes) of one
 * unchanged state on screen, then again every period while it stays. Application-owned: the
 * animation runtime has no timer. Pure (time is passed in, monotonic milliseconds); time paused in
 * the background does not count.
 */
class LongIdleSchedule(
    private val periodMillis: Long = DEFAULT_PERIOD_MILLIS,
) {
    private var shown: Expression? = null
    private var since: Long? = null

    init {
        require(periodMillis > 0) { "period" }
    }

    /** The state on screen at [now]; a different one restarts the count. */
    fun observe(
        expression: Expression,
        now: Long,
    ) {
        if (expression != shown) {
            shown = expression
            since = now
        }
    }

    fun nextAt(): Long? = since?.let { it + periodMillis }

    fun due(now: Long): Boolean = nextAt()?.let { now >= it } == true

    /** The action played at [now]: the next one is a full period later. */
    fun fired(now: Long) {
        if (since != null) since = now
    }

    /** Off screen: nothing counts until [resume]. */
    fun pause() {
        since = null
    }

    fun resume(now: Long) {
        if (shown != null) since = now
    }

    companion object {
        const val DEFAULT_PERIOD_MILLIS = 600_000L
    }
}
