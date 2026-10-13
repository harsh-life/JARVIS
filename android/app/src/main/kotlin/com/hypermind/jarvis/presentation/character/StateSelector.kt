package com.hypermind.jarvis.presentation.character

import java.time.Duration
import java.time.Instant

/** What the selector chose, and with which score (the controller needs it to judge urgency). */
data class Selection(
    val expression: Expression,
    val score: Int,
)

/**
 * The deterministic selection policy (OD-CHAR-2; the same policy as
 * `select_expression` in `shared/schemas/presentation.py`, held to it by
 * `shared/android/presentation_selection_vectors.json`):
 *
 * 1. validate the result (raw provider output is parsed strictly);
 * 2. a malformed, missing or stale result selects [Expression.DEFAULT];
 * 3. an uncertain result, or one below the confidence floor, selects DEFAULT;
 * 4. a state is eligible only if its score reaches its own threshold;
 * 5. the highest-scoring eligible state is selected;
 * 6. ties go to the earlier state in [Expression]'s order;
 * 7. with no eligible state, DEFAULT.
 */
object StateSelector {
    private val FUTURE_TOLERANCE: Duration = Duration.ofSeconds(1)

    fun select(
        raw: String?,
        settings: CharacterSettings,
        now: Instant? = null,
    ): Selection = select(raw?.let(InferenceResult::parse), settings, now)

    fun select(
        result: InferenceResult?,
        settings: CharacterSettings,
        now: Instant? = null,
    ): Selection {
        val fallback = Selection(Expression.DEFAULT, 0)
        if (result == null) return fallback
        if (now != null) {
            val age = Duration.between(result.timestamp, now)
            if (age > Duration.ofMillis(settings.maxResultAgeMillis) ||
                age < FUTURE_TOLERANCE.negated()
            ) {
                return fallback
            }
        }
        if (result.uncertain || result.confidence < settings.minConfidence) return fallback
        var best: Expression? = null
        for (expression in Expression.entries) {
            val score = result.score(expression)
            if (score < settings.threshold(expression)) continue
            if (best == null || score > result.score(best)) best = expression
        }
        return best?.let { Selection(it, result.score(it)) } ?: fallback
    }
}
