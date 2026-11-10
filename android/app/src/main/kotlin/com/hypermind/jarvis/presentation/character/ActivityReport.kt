package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * One activity report from the server's activity scorer (OD-CHAR-4; mirrors `ActivityPresentation` in
 * `shared/schemas/presentation.py`, held to it by `shared/android/presentation_activity_samples.json`):
 * a 1–10 score per state for how well it presents JARVIS's current operational activity. The
 * components the server computed them from (engagement, productivity, failure, pressure …) are
 * validated with the rest and then dropped: this client needs only the scores, and keeps nothing.
 *
 * A score describes JARVIS's work, never the user, and is not a claim that JARVIS has feelings.
 */
class ActivityReport private constructor(
    private val scores: Map<Expression, Int>,
) {
    fun score(expression: Expression): Int = scores.getValue(expression)

    companion object {
        private val FIELDS = setOf("schema_version", "computed_at", "window_s", "scores", "components")
        private val COMPONENTS =
            setOf("engagement", "productivity", "steadiness", "failure", "pressure", "surprise", "inactivity")
        private val COMPONENT_RANGE = 0..100
        private val WINDOW_RANGE = 60..86_400

        /** Scores for a test or a preview; validated like a parsed report. */
        fun of(scores: Map<Expression, Int>): ActivityReport {
            require(scores.keys == Expression.entries.toSet()) { "a score is missing" }
            require(scores.values.all { it in CharacterSettings.SCORE_RANGE }) { "a score is out of range" }
            return ActivityReport(scores.toMap())
        }

        /**
         * The server's response body, validated whole: a missing, extra, mistyped or out-of-range
         * field — or anything that is not a report — rejects it (null).
         */
        fun parse(text: String?): ActivityReport? =
            try {
                val root = Json.parseToJsonElement(requireNotNull(text)).jsonObject
                require(root.keys == FIELDS) { "fields" }
                require(CharacterSettings.int(root.getValue("schema_version")) == 1) { "schema" }
                OffsetDateTime.parse(CharacterSettings.string(root.getValue("computed_at")))
                require(CharacterSettings.int(root.getValue("window_s")) in WINDOW_RANGE) { "window" }
                val components = obj(root.getValue("components"))
                require(components.keys == COMPONENTS) { "components" }
                require(components.values.all { CharacterSettings.int(it) in COMPONENT_RANGE }) { "component" }
                val scores = obj(root.getValue("scores"))
                require(scores.keys == Expression.entries.map { it.wire }.toSet()) { "scores" }
                of(Expression.entries.associateWith { CharacterSettings.int(scores.getValue(it.wire)) })
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            } catch (ignored: ClassCastException) {
                null
            } catch (ignored: DateTimeParseException) {
                null
            }

        private fun obj(element: JsonElement): JsonObject = element as JsonObject
    }
}
