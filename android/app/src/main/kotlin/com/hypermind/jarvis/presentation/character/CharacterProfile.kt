package com.hypermind.jarvis.presentation.character

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject

/**
 * Which image shows each [Expression] for one character (OD-CHAR-1; the
 * `profile.json` of `assets/character/<id>/`, schema in
 * `shared/schemas/presentation.py`). The mapping is data, so a profile
 * chosen later (e.g. from the configuration website) needs no code change.
 * A state the profile omits shows [fallback].
 */
data class CharacterProfile(
    val profileId: String,
    val fallback: Expression,
    val states: Map<Expression, String>,
) {
    init {
        require(PROFILE_ID.matches(profileId)) { "bad profile id" }
        require(states.values.all { ASSET_NAME.matches(it) }) { "an asset is not a bare .png/.webp name" }
        require(fallback in states) { "the fallback state has no image" }
    }

    /** The image file for [expression]; the fallback's when the profile has none for it. */
    fun assetFor(expression: Expression): String = states[expression] ?: states.getValue(fallback)

    /** The image's path inside the app's assets. */
    fun assetPath(asset: String): String = "$ASSET_ROOT/$profileId/$asset"

    companion object {
        const val ASSET_ROOT = "character"
        const val PROFILE_FILE = "profile.json"
        private const val SCHEMA_VERSION = 1
        private val PROFILE_ID = Regex("^[a-z0-9][a-z0-9_-]{0,63}$")
        private val ASSET_NAME = Regex("^[a-z0-9][a-z0-9_-]{0,63}\\.(png|webp)$")
        private val FIELDS = setOf("schema_version", "profile_id", "fallback", "states")

        /**
         * The built-in default character — the same mapping as
         * `assets/character/default/profile.json` (a test holds them equal). Used
         * whenever no valid profile can be read, so the character never depends
         * on a file being well formed.
         */
        val BUILT_IN =
            CharacterProfile(
                profileId = "default",
                fallback = Expression.DEFAULT,
                states =
                    mapOf(
                        Expression.DEFAULT to "default.png",
                        Expression.SATISFIED to "satisfied.webp",
                        Expression.HAPPY to "happy.webp",
                        Expression.SLEEPY to "sleepy.webp",
                        Expression.SURPRISED to "surprised.webp",
                        Expression.ANGRY to "angry.webp",
                        Expression.SAD to "sad.webp",
                    ),
            )

        /**
         * A profile document, validated as strictly as the server's schema: an
         * unknown field, an unknown state, a path, a non-image name or an
         * unmapped fallback rejects the whole profile (null), never part of it.
         */
        fun parse(text: String): CharacterProfile? =
            try {
                val root = Json.parseToJsonElement(text).jsonObject
                require(root.keys.all { it in FIELDS }) { "unknown field" }
                val version = root["schema_version"]?.let { (it as JsonPrimitive).int } ?: SCHEMA_VERSION
                require(version == SCHEMA_VERSION) { "unsupported schema" }
                val states =
                    (root.getValue("states") as JsonObject).entries.associate { (key, value) ->
                        val state = Expression.entries.firstOrNull { it.wire == key }
                        requireNotNull(state) { "unknown state" }
                        state to string(value)
                    }
                require(states.isNotEmpty()) { "no states" }
                val fallback = root["fallback"]?.let { string(it) } ?: Expression.DEFAULT.wire
                CharacterProfile(
                    profileId = string(root.getValue("profile_id")),
                    fallback = Expression.entries.firstOrNull { it.wire == fallback } ?: error("unknown fallback"),
                    states = states,
                )
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            } catch (ignored: IllegalStateException) {
                null
            } catch (ignored: NoSuchElementException) {
                null
            } catch (ignored: ClassCastException) {
                null
            }

        private fun string(element: JsonElement): String {
            val primitive = element as JsonPrimitive
            require(primitive.isString) { "not a string" }
            return primitive.content
        }
    }
}
