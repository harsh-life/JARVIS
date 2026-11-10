package com.hypermind.jarvis.presentation.character

/**
 * The central, testable state controller (OD-CHAR-4): decides which expression is shown, and how
 * strongly, from the server's activity reports, local activity and the clock. Pure — time is passed
 * in (monotonic milliseconds) — so every rule is tested without waiting.
 *
 * The decision itself is the [ActivityPolicy] (hysteresis, minimum display, cooldowns, staleness,
 * the local wake rule and the intensity mapping), the one system that selects an expression. This
 * class adds only the development preview: a pinned state that nothing else changes until it is
 * unpinned, while reports and activity keep updating the policy underneath.
 *
 * It holds no task, connection or error state and acts on nothing: what it receives is a report of
 * scores, already reduced to numbers by the server, and it commits a face.
 */
class CharacterController(
    private val settings: CharacterSettings,
    now: Long,
) {
    private val policy = ActivityPolicy(settings, now)
    private var pin: PresentedCharacter? = null

    /** The committed expression and its intensity. */
    val presented: PresentedCharacter get() = pin ?: policy.presented

    /** The committed expression ([presented]'s). */
    val shown: Expression get() = presented.expression

    /** The user did something here (opening the app, voice input, a request): wakes a sleepy character. */
    fun onActivity(now: Long) = policy.onLocalActivity(now)

    /** A server activity report, or null for one that failed validation (discarded). */
    fun onReport(
        report: ActivityReport?,
        now: Long,
    ) = policy.onReport(report, now)

    /** Applies whatever is due at [now]. */
    fun tick(now: Long) = policy.tick(now)

    /** When [tick] may next change something, or null if only new input can. */
    fun nextChangeAt(now: Long): Long? = if (pin != null) null else policy.nextChangeAt(now)

    /**
     * Development preview: show [expression] at [intensity] (neutral unless chosen) until [unpin];
     * nothing else changes it meanwhile.
     */
    fun pin(
        expression: Expression,
        now: Long,
        intensity: Int = IntensityPolicy.NEUTRAL,
    ) {
        pin = PresentedCharacter(expression, intensity)
        policy.tick(now)
    }

    fun unpin(now: Long) {
        pin = null
        policy.tick(now)
    }
}
