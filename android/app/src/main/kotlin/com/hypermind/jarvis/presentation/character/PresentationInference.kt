package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.jsonObject
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * What an inference provider may base its scores on (OD-CHAR-2; mirrors
 * `SignalType` in `shared/schemas/presentation.py`). There is no raw-audio
 * signal: [PROSODY] means features derived from the current utterance.
 */
enum class SignalType(
    val wire: String,
) {
    TEXT("text"),
    TRANSCRIPT("transcript"),
    PROSODY("prosody"),
    CONTEXT("context"),
    EVENT("event"),
}

/**
 * Features of the current utterance's audio, if a voice front end supplies
 * them — numbers only, never audio. Loudness is not emotional intensity: a
 * provider must not read a loud utterance as any particular state on its own.
 */
data class ProsodyFeatures(
    /** Peak level relative to the utterance's own average, in dB. */
    val peakOverMeanDb: Float,
)

/** A verified application event (never inferred, never invented). */
sealed interface PresentationEvent {
    /** A task the user started has just completed (from the server's own answer). */
    data object TaskCompleted : PresentationEvent
}

/**
 * The current interaction, as a provider sees it. In memory only: it is never
 * persisted, logged or sent anywhere, and [context] is at most the last few
 * turns of the current session ([CharacterPresentation.MAX_CONTEXT]).
 */
data class InferenceInput(
    val utterance: String? = null,
    val spoken: Boolean = false,
    val prosody: ProsodyFeatures? = null,
    val context: List<String> = emptyList(),
    val events: List<PresentationEvent> = emptyList(),
)

/**
 * One provider result, validated on construction (mirrors
 * `PresentationInference`): a 1–10 score for every state, a confidence in
 * 0–1, a timestamp, the signal types used and an uncertainty flag. A score is
 * the provider's estimate that a state is an appropriate *presentation* now —
 * not a measurement of anyone's emotions.
 */
data class InferenceResult(
    val scores: Map<Expression, Int>,
    val confidence: Double,
    val timestamp: Instant,
    val signals: Set<SignalType>,
    val uncertain: Boolean,
) {
    init {
        require(scores.keys == Expression.entries.toSet()) { "a score is missing" }
        require(scores.values.all { it in CharacterSettings.SCORE_RANGE }) { "a score is out of range" }
        require(confidence.isFinite() && confidence in 0.0..1.0) { "confidence is out of range" }
    }

    fun score(expression: Expression): Int = scores.getValue(expression)

    companion object {
        private val FIELDS =
            Expression.entries.map { "${it.wire}_score" }.toSet() +
                setOf("confidence", "timestamp", "signals", "uncertain")

        /**
         * A provider's raw output (e.g. a model's JSON), validated whole: a
         * missing, extra, mistyped or out-of-range field rejects it (null).
         */
        fun parse(text: String): InferenceResult? =
            try {
                val root = Json.parseToJsonElement(text).jsonObject
                require(root.keys == FIELDS) { "fields" }
                val signals = (root.getValue("signals") as JsonArray).map(CharacterSettings::string)
                require(signals.size == signals.toSet().size) { "repeated signal" }
                InferenceResult(
                    scores =
                        Expression.entries.associateWith {
                            CharacterSettings.int(
                                root.getValue("${it.wire}_score"),
                            )
                        },
                    confidence = CharacterSettings.number(root.getValue("confidence")),
                    timestamp = OffsetDateTime.parse(CharacterSettings.string(root.getValue("timestamp"))).toInstant(),
                    signals =
                        signals
                            .map { wire ->
                                requireNotNull(SignalType.entries.firstOrNull { it.wire == wire }) { "unknown signal" }
                            }.toSet(),
                    uncertain = CharacterSettings.bool(root.getValue("uncertain")),
                )
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            } catch (ignored: ClassCastException) {
                null
            } catch (ignored: DateTimeParseException) {
                null
            }
    }
}

/**
 * The replaceable source of scores (OD-CHAR-2). Runs on the endpoint; the
 * renderer and the selector depend on this interface only, never on a model
 * vendor or inference library. Returns null when it has nothing to offer
 * (the character then stays at its baseline); a provider that throws is
 * treated the same way.
 */
fun interface PresentationInferenceProvider {
    suspend fun infer(input: InferenceInput): InferenceResult?
}
