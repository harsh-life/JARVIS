package com.hypermind.jarvis.presentation.character

/**
 * The central, testable state controller (OD-CHAR-2): decides which
 * expression is shown and when, from selections, user activity and the clock.
 * Pure — time is passed in (monotonic milliseconds) — so every rule below is
 * tested without waiting.
 *
 * - The **baseline** is [Expression.DEFAULT], or [Expression.SLEEPY] once
 *   nothing has happened for `idleTimeoutMillis`. It starts at DEFAULT.
 * - A **selected** state (anything the selector chose but DEFAULT) is shown
 *   for its hold time, then the baseline returns: no selected state stays
 *   indefinitely. A DEFAULT selection (insufficient evidence) changes nothing.
 * - **Minimum display:** a selected state is not replaced by another sooner
 *   than `minDisplayMillis`; meanwhile only the latest candidate is kept
 *   (rapid predictions collapse into one change). Leaving the baseline is
 *   immediate.
 * - **Urgent:** a selection scoring at least `interruptScore` replaces the
 *   shown state at once, even mid-transition.
 * - **Wake:** activity while idle-sleepy returns to DEFAULT at once.
 *
 * It holds no task, connection or error state and decides nothing else.
 */
class CharacterController(
    private val settings: CharacterSettings,
    now: Long,
) {
    var shown: Expression = Expression.DEFAULT
        private set
    private var shownSince = now
    private var selected = false
    private var lastActivity = now
    private var pending: Selection? = null
    private var pinned = false

    /** DEFAULT, or SLEEPY after the idle timeout. */
    fun baseline(now: Long): Expression =
        if (now - lastActivity >= settings.idleTimeoutMillis) Expression.SLEEPY else Expression.DEFAULT

    /** The user did something meaningful (a request, voice, opening the app). */
    fun onActivity(now: Long) {
        lastActivity = now
        if (!pinned && !selected && shown == Expression.SLEEPY) show(Expression.DEFAULT, now, selected = false)
    }

    fun onSelection(
        selection: Selection,
        now: Long,
    ) {
        if (pinned || selection.expression == Expression.DEFAULT) return
        val urgent = selection.score >= settings.interruptScore
        val settled = !selected || now - shownSince >= settings.minDisplayMillis
        if (urgent || settled || selection.expression == shown) {
            pending = null
            show(selection.expression, now, selected = true)
        } else {
            pending = selection
        }
    }

    /** Applies whatever is due at [now]. */
    fun tick(now: Long) {
        if (pinned) return
        val waiting = pending
        val held = now - shownSince
        // A waiting candidate goes up once the shown state has had its minimum
        // display — or its hold ran out first (it would otherwise wait again
        // behind the baseline).
        if (waiting != null && (held >= settings.minDisplayMillis || held >= settings.hold(shown))) {
            pending = null
            show(waiting.expression, now, selected = true)
            return
        }
        if (selected && held >= settings.hold(shown)) {
            show(baseline(now), now, selected = false)
        } else if (!selected && shown != baseline(now)) {
            show(baseline(now), now, selected = false)
        }
    }

    /** When [tick] may next change something, or null if only new input can. */
    fun nextChangeAt(): Long? {
        if (pinned) return null
        return listOfNotNull(
            pending?.let { saturating(shownSince, minOf(settings.minDisplayMillis, settings.hold(shown))) },
            if (selected) saturating(shownSince, settings.hold(shown)) else null,
            if (!selected && shown != Expression.SLEEPY) saturating(lastActivity, settings.idleTimeoutMillis) else null,
        ).minOrNull()
    }

    /** Development preview: show [expression] until [unpin]; nothing else changes it meanwhile. */
    fun pin(
        expression: Expression,
        now: Long,
    ) {
        pinned = true
        pending = null
        show(expression, now, selected = false)
    }

    fun unpin(now: Long) {
        pinned = false
        show(baseline(now), now, selected = false)
    }

    private fun show(
        expression: Expression,
        now: Long,
        selected: Boolean,
    ) {
        shown = expression
        shownSince = now
        this.selected = selected
    }

    private fun saturating(
        start: Long,
        plus: Long,
    ): Long = if (plus > Long.MAX_VALUE - start) Long.MAX_VALUE else start + plus
}
