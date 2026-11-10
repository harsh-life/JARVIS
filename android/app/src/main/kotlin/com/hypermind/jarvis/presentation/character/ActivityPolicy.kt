package com.hypermind.jarvis.presentation.character

/**
 * The deterministic presentation policy (OD-CHAR-4; the same policy as `ActivityPolicy` in
 * `shared/schemas/presentation.py`, held to it by
 * `shared/android/presentation_activity_policy_vectors.json`). It turns a stream of server activity
 * reports, local activity and the clock into one committed [PresentedCharacter].
 *
 * Pure: time is passed in (monotonic milliseconds). In memory only — nothing is stored, logged or
 * sent. At each report, local activity or tick:
 *
 * 1. **Evidence.** The last valid report is evidence until `max_age_ms` after it was *received*. An
 *    invalid report is discarded and changes nothing.
 * 2. **No evidence** (signed out, offline, stale): the baseline only — DEFAULT, or SLEEPY once there
 *    has been no local activity for the idle timeout — at the baselines' fixed intensities.
 * 3. **Local wake.** Local activity within the idle timeout makes SLEEPY ineligible, whatever the
 *    report says.
 * 4. **Enter** a non-default state only at its threshold, and not while it cools down.
 * 5. **Hysteresis.** The shown state stays while its score is at least its threshold minus the exit
 *    margin — except SURPRISED, which leaves after its hold.
 * 6. **Choice.** The highest-scoring eligible state; the shown state wins a tie, otherwise the
 *    owner's order. Nothing eligible: DEFAULT.
 * 7. **Minimum display.** While the shown state still holds, a different choice replaces it only
 *    after the minimum display, unless it scores at least the interrupt score.
 * 8. **Intensity.** `forSelection(max(score, threshold), threshold, floor)`; a baseline without
 *    evidence at its fixed intensity; the shown state keeps its intensity inside the dead band.
 */
class ActivityPolicy(
    private val settings: CharacterSettings,
    now: Long,
) {
    var presented: PresentedCharacter =
        PresentedCharacter(Expression.DEFAULT, settings.baselineIntensity(Expression.DEFAULT))
        private set

    val shown: Expression get() = presented.expression

    private var since = now
    private var report: ActivityReport? = null
    private var receivedAt: Long? = null
    private var localAt = now
    private val lastExit = mutableMapOf<Expression, Long>()

    /** A report from the server; null (unparseable or invalid) is discarded. */
    fun onReport(
        next: ActivityReport?,
        now: Long,
    ) {
        if (next != null) {
            report = next
            receivedAt = now
        }
        evaluate(now)
    }

    /** The user did something here: opened the app, started voice input, submitted a request. */
    fun onLocalActivity(now: Long) {
        localAt = now
        evaluate(now)
    }

    fun tick(now: Long) = evaluate(now)

    /** The earliest time a [tick] could change something without new input, or null. */
    fun nextChangeAt(now: Long): Long? {
        val times = mutableListOf(saturating(localAt, settings.idleTimeoutMillis))
        receivedAt?.let { times += saturating(it, settings.activityMaxAgeMillis + 1) }
        if (shown != Expression.DEFAULT) times += saturating(since, settings.minDisplayMillis)
        if (shown in TRANSIENT) times += saturating(since, settings.hold(shown))
        for ((state, left) in lastExit) times += saturating(left, settings.cooldown(state))
        return times.filter { it > now }.minOrNull()
    }

    private fun evaluate(now: Long) {
        val scores = evidence(now)
        if (scores == null) {
            // Rule 2: no evidence — the baseline only.
            val idle = now - localAt >= settings.idleTimeoutMillis
            val target = if (idle) Expression.SLEEPY else Expression.DEFAULT
            commit(target, settings.baselineIntensity(target), now)
            return
        }
        val current = shown
        val holds = holds(current, scores, now)
        var target = choose(current, holds, scores, now)
        if (target != current && holds) {
            // Rule 7: a state that still holds yields only after its minimum display, or to an urgent one.
            val settled = now - since >= settings.minDisplayMillis
            if (!settled && scores.getValue(target) < settings.interruptScore) target = current
        }
        commit(target, intensityFor(target, scores), now)
    }

    /** Rules 1 and 3: the fresh report's scores, with SLEEPY silenced by recent local activity; or null. */
    private fun evidence(now: Long): Map<Expression, Int>? {
        val evidence = report ?: return null
        val received = receivedAt ?: return null
        if (now - received > settings.activityMaxAgeMillis) return null
        val scores = Expression.entries.associateWith { evidence.score(it) }.toMutableMap()
        if (now - localAt < settings.idleTimeoutMillis) scores[Expression.SLEEPY] = SCORE_MIN
        return scores
    }

    /** Rule 5: the shown state stays down to its threshold minus the margin; SURPRISED until its hold ends. */
    private fun holds(
        current: Expression,
        scores: Map<Expression, Int>,
        now: Long,
    ): Boolean {
        if (current == Expression.DEFAULT) return false
        val expired = current in TRANSIENT && now - since >= settings.hold(current)
        return !expired && scores.getValue(current) >= settings.threshold(current) - settings.activityExitMargin
    }

    /** Rules 4 and 6: the best eligible state; the shown one wins a tie, then the owner's order. */
    private fun choose(
        current: Expression,
        holds: Boolean,
        scores: Map<Expression, Int>,
        now: Long,
    ): Expression {
        var best: Expression? = if (holds) current else null
        // Declaration order = tie-break order; a later state must score strictly higher.
        for (state in Expression.entries.filter { it != current && enterable(it, scores, now) }) {
            if (best == null || scores.getValue(state) > scores.getValue(best)) best = state
        }
        return best ?: Expression.DEFAULT
    }

    private fun enterable(
        state: Expression,
        scores: Map<Expression, Int>,
        now: Long,
    ): Boolean =
        state != Expression.DEFAULT && scores.getValue(state) >= settings.threshold(state) && !cooling(state, now)

    private fun cooling(
        state: Expression,
        now: Long,
    ): Boolean {
        val left = lastExit[state] ?: return false
        return now - left < settings.cooldown(state)
    }

    private fun intensityFor(
        state: Expression,
        scores: Map<Expression, Int>,
    ): Int {
        if (state == Expression.DEFAULT) return settings.baselineIntensity(state)
        val threshold = settings.threshold(state)
        return IntensityPolicy.forSelection(
            maxOf(scores.getValue(state), threshold),
            threshold,
            settings.intensityFloor,
        )
    }

    private fun commit(
        target: Expression,
        intensity: Int,
        now: Long,
    ) {
        if (target == shown) {
            val kept = IntensityPolicy.retained(presented.intensity, intensity, settings.intensityDeadBand)
            presented = PresentedCharacter(target, kept)
            return
        }
        if (shown != Expression.DEFAULT) lastExit[shown] = now
        presented = PresentedCharacter(target, intensity)
        since = now
    }

    private fun saturating(
        start: Long,
        plus: Long,
    ): Long = if (plus > Long.MAX_VALUE - start) Long.MAX_VALUE else start + plus

    companion object {
        /** States with a maximum display time: an event, not a condition. */
        val TRANSIENT = setOf(Expression.SURPRISED)

        private const val SCORE_MIN = 1
    }
}
