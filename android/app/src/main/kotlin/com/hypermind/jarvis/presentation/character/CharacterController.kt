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
 * - **Intensity:** what it commits is a [PresentedCharacter], the expression *and* how strongly to
 *   present it, decided together. A selected state's intensity comes from its suitability score
 *   through [IntensityPolicy] (threshold-relative); a waiting candidate carries its own. A
 *   baseline state has no score, so it takes its fixed intensity from the settings (DEFAULT 5,
 *   SLEEPY 2) — no score is made up for it. The shown state selected again keeps its intensity
 *   unless the new one differs by at least the dead band. A selection whose score does not meet
 *   its own threshold (the selector never makes one) changes nothing.
 *
 * It holds no task, connection or error state and decides nothing else.
 */
class CharacterController(
    private val settings: CharacterSettings,
    now: Long,
) {
    /** The committed expression and its intensity. */
    var presented: PresentedCharacter =
        PresentedCharacter(Expression.DEFAULT, settings.baselineIntensity(Expression.DEFAULT))
        private set

    /** The committed expression ([presented]'s). */
    val shown: Expression get() = presented.expression

    private var shownSince = now
    private var selected = false
    private var lastActivity = now
    private var pending: PresentedCharacter? = null
    private var pinned = false

    /** DEFAULT, or SLEEPY after the idle timeout. */
    fun baseline(now: Long): Expression =
        if (now - lastActivity >= settings.idleTimeoutMillis) Expression.SLEEPY else Expression.DEFAULT

    /** The user did something meaningful (a request, voice, opening the app). */
    fun onActivity(now: Long) {
        lastActivity = now
        if (!pinned && !selected && shown == Expression.SLEEPY) showBaseline(Expression.DEFAULT, now)
    }

    fun onSelection(
        selection: Selection,
        now: Long,
    ) {
        if (pinned || selection.expression == Expression.DEFAULT) return
        val intensity =
            IntensityPolicy.forSelectionOrNull(
                selection.score,
                settings.threshold(selection.expression),
                settings.intensityFloor,
            ) ?: return
        val urgent = selection.score >= settings.interruptScore
        val settled = !selected || now - shownSince >= settings.minDisplayMillis
        if (selection.expression == shown) {
            pending = null
            val kept = IntensityPolicy.retained(presented.intensity, intensity, settings.intensityDeadBand)
            show(PresentedCharacter(shown, kept), now, selected = true)
        } else if (urgent || settled) {
            pending = null
            show(PresentedCharacter(selection.expression, intensity), now, selected = true)
        } else {
            pending = PresentedCharacter(selection.expression, intensity)
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
            show(waiting, now, selected = true)
            return
        }
        if (selected && held >= settings.hold(shown)) {
            showBaseline(baseline(now), now)
        } else if (!selected && shown != baseline(now)) {
            showBaseline(baseline(now), now)
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

    /**
     * Development preview: show [expression] at [intensity] (neutral unless chosen) until [unpin];
     * nothing else changes it meanwhile.
     */
    fun pin(
        expression: Expression,
        now: Long,
        intensity: Int = IntensityPolicy.NEUTRAL,
    ) {
        pinned = true
        pending = null
        show(PresentedCharacter(expression, intensity), now, selected = false)
    }

    fun unpin(now: Long) {
        pinned = false
        showBaseline(baseline(now), now)
    }

    private fun showBaseline(
        expression: Expression,
        now: Long,
    ) = show(PresentedCharacter(expression, settings.baselineIntensity(expression)), now, selected = false)

    private fun show(
        next: PresentedCharacter,
        now: Long,
        selected: Boolean,
    ) {
        presented = next
        shownSince = now
        this.selected = selected
    }

    private fun saturating(
        start: Long,
        plus: Long,
    ): Long = if (plus > Long.MAX_VALUE - start) Long.MAX_VALUE else start + plus
}
