package com.hypermind.jarvis.presentation.character

import java.time.Instant

/**
 * The pilot's [PresentationInferenceProvider] (OD-CHAR-2): a small,
 * deterministic, on-device cue scorer. **It is not a trained or learned
 * model** — a fixed English cue list plus verified events, in the same spirit
 * as the memory gate's emotion lexicon (OD-MB-3). It downloads nothing, has
 * no dependency and runs in microseconds; a fine-tuned small model can
 * replace it behind the same interface later.
 *
 * Rules it keeps:
 * - Insufficient evidence (no cue) is reported as uncertain, so the selector
 *   falls back to DEFAULT.
 * - A negated cue ("not great") does not count.
 * - Positive and negative cues at once (e.g. "thanks" and "unfortunately")
 *   are reported as uncertain; compatible cues compete on score and order.
 * - Loudness alone never reaches a threshold: a loud utterance without a
 *   surprise cue scores SURPRISED below any sensible threshold and is
 *   uncertain; with one, it only adds a point.
 * - SLEEPY is never scored from words: it comes from the idle policy.
 * - Only the current utterance and verified events are read; [InferenceInput.context]
 *   is not used, so CONTEXT is never reported.
 */
class LocalCueScorer(
    private val clock: () -> Instant = Instant::now,
) : PresentationInferenceProvider {
    override suspend fun infer(input: InferenceInput): InferenceResult = score(input)

    fun score(input: InferenceInput): InferenceResult {
        val signals = mutableSetOf<SignalType>()
        val cued = mutableMapOf<Expression, Int>()
        val text = input.utterance?.takeIf { it.isNotBlank() }
        if (text != null) {
            signals += if (input.spoken) SignalType.TRANSCRIPT else SignalType.TEXT
            for ((state, score) in textCues(text)) cued.merge(state, score, ::maxOf)
        }
        if (PresentationEvent.TaskCompleted in input.events) {
            signals += SignalType.EVENT
            cued.merge(Expression.SATISFIED, EVENT_SCORE, ::maxOf)
        }
        var loudOnly = false
        input.prosody?.let { prosody ->
            signals += SignalType.PROSODY
            if (prosody.peakOverMeanDb >= LOUD_DB) {
                val surprise = cued[Expression.SURPRISED]
                if (surprise != null) {
                    cued[Expression.SURPRISED] = minOf(surprise + 1, MAX)
                } else if (cued.isEmpty()) {
                    loudOnly = true
                }
            }
        }
        val scores = Expression.entries.associateWith { MIN }.toMutableMap()
        scores[Expression.DEFAULT] = NEUTRAL
        scores.putAll(cued)
        if (loudOnly) scores[Expression.SURPRISED] = LOUD_ALONE
        val strong = cued.filterValues { it >= STRONG }.keys
        val conflict = strong.any { it in POSITIVE } && strong.any { it in NEGATIVE }
        val (confidence, uncertain) =
            when {
                cued.isEmpty() -> NONE_CONFIDENCE to true
                conflict -> CONFLICT_CONFIDENCE to true
                SignalType.EVENT in signals && text != null -> AGREE_CONFIDENCE to false
                SignalType.EVENT in signals -> EVENT_CONFIDENCE to false
                else -> TEXT_CONFIDENCE to false
            }
        return InferenceResult(scores, confidence, clock(), signals, uncertain)
    }

    private fun textCues(text: String): Map<Expression, Int> {
        val lower = text.lowercase()
        val tokens = TOKEN.findAll(lower).map { it.value }.toList()
        val found = mutableMapOf<Expression, Int>()
        for ((state, cues) in CUES) {
            for (cue in cues) {
                val hit = if (cue.emoji) lower.contains(cue.text) else matches(tokens, cue.tokens)
                if (hit) found.merge(state, cue.score, ::maxOf)
            }
        }
        if (tokens.any { '?' in it && '!' in it }) found.merge(Expression.SURPRISED, INTERROBANG, ::maxOf)
        return found
    }

    /** The phrase occurs, and no negation sits in the few tokens before it. */
    private fun matches(
        tokens: List<String>,
        phrase: List<String>,
    ): Boolean =
        (0..tokens.size - phrase.size).any { start ->
            tokens.subList(start, start + phrase.size) == phrase &&
                tokens.subList(maxOf(0, start - NEGATION_WINDOW), start).none { it in NEGATIONS || it.endsWith("n't") }
        }

    private class Cue(
        val text: String,
        val score: Int,
    ) {
        val emoji = text.none { it.isLetterOrDigit() || it == ' ' || it == '\'' }
        val tokens = text.split(' ')
    }

    private companion object {
        const val MIN = 1
        const val MAX = 10
        const val NEUTRAL = 6
        const val STRONG = 7
        const val EVENT_SCORE = 8
        const val INTERROBANG = 7
        const val LOUD_ALONE = 4
        const val LOUD_DB = 12f
        const val NEGATION_WINDOW = 3
        const val NONE_CONFIDENCE = 0.3
        const val CONFLICT_CONFIDENCE = 0.4
        const val TEXT_CONFIDENCE = 0.7
        const val EVENT_CONFIDENCE = 0.8
        const val AGREE_CONFIDENCE = 0.85
        val TOKEN = Regex("[a-z0-9']+|[?!]+")
        val NEGATIONS = setOf("not", "no", "never", "hardly", "nothing")
        val POSITIVE = setOf(Expression.HAPPY, Expression.SATISFIED)
        val NEGATIVE = setOf(Expression.ANGRY, Expression.SAD)

        fun cues(
            score: Int,
            vararg phrases: String,
        ) = phrases.map { Cue(it, score) }

        val CUES: Map<Expression, List<Cue>> =
            mapOf(
                Expression.HAPPY to
                    cues(
                        8,
                        "yay",
                        "hooray",
                        "woohoo",
                        "awesome",
                        "amazing",
                        "fantastic",
                        "wonderful",
                        "brilliant",
                        "love it",
                        "love this",
                        "so happy",
                        "great news",
                        "so excited",
                        "🎉",
                        "🥳",
                        "😄",
                        "😀",
                    ),
                Expression.SATISFIED to
                    cues(
                        7,
                        "thanks",
                        "thank you",
                        "perfect",
                        "that works",
                        "that worked",
                        "well done",
                        "good job",
                        "nice one",
                        "exactly right",
                        "👍",
                    ),
                Expression.SURPRISED to
                    cues(
                        8,
                        "wow",
                        "whoa",
                        "woah",
                        "no way",
                        "omg",
                        "oh my god",
                        "unbelievable",
                        "can't believe",
                        "😮",
                        "😲",
                    ),
                Expression.ANGRY to
                    cues(
                        8,
                        "ugh",
                        "annoying",
                        "so annoyed",
                        "so frustrating",
                        "frustrated",
                        "that's wrong",
                        "not what i asked",
                        "come on",
                        "😤",
                        "😠",
                    ),
                Expression.SAD to
                    cues(
                        8,
                        "sad",
                        "unfortunately",
                        "bad news",
                        "passed away",
                        "heartbroken",
                        "i'm sorry",
                        "so sorry",
                        "😢",
                        "😭",
                    ),
            )
    }
}
