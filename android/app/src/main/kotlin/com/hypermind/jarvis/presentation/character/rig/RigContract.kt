package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * The animated character's control contract (`character/rig/JarvisCharacter.contract.json`,
 * written by the rig project with its exports): which animation plays each state, its long idle,
 * the entry into it, the five transitions whose starting state changes the motion, the entrance
 * and exit, and the crossfade times. It names animations; it decides nothing — the
 * `CharacterController` chooses the state.
 */
class RigContract private constructor(
    val idle: Map<Expression, String>,
    val longIdle: Map<Expression, String>,
    val entry: Map<Expression, String>,
    val pairs: Map<Pair<Expression, Expression>, String>,
    val enter: String,
    val exit: String,
    val mix: Mix,
    /** Every animation of the rig, with its duration (seconds). */
    val durations: Map<String, Float>,
) {
    /** Crossfade seconds (`mix_seconds`). */
    data class Mix(
        val intoEntry: Float,
        val intoPair: Float,
        val toIdle: Float,
        val intoLongIdle: Float,
        val longIdleToIdle: Float,
        val interrupt: Float,
    )

    /** Every animation the contract names. */
    val animationNames: Set<String> =
        (idle.values + longIdle.values + entry.values + pairs.values + listOf(enter, exit)).toSet()

    /** The contract's transition rule: the pair's own animation if it has one, else the target's entry. */
    fun transition(
        from: Expression,
        to: Expression,
    ): String = pairs[from to to] ?: entry.getValue(to)

    companion object {
        const val SCHEMA = "jarvis-character-rig-contract/1"
        private val MIX_RANGE = 0.0..1.0
        private val json = Json { ignoreUnknownKeys = true }

        /** A contract, validated whole: anything malformed or incomplete is null (the caller falls back). */
        fun parse(text: String): RigContract? =
            try {
                read(json.parseToJsonElement(text).jsonObject)
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            } catch (ignored: IllegalStateException) {
                null
            } catch (ignored: NoSuchElementException) {
                null
            }

        /** Throws IllegalArgumentException (via require) on anything invalid; [parse] maps that to null. */
        private fun read(root: JsonObject): RigContract {
            require(root.string("schema") == SCHEMA) { "schema" }
            val states = root.getValue("states").jsonArray.map { it.jsonPrimitive.content }
            val wires = Expression.entries.map { it.wire }
            require(states.toSet() == wires.toSet() && states.size == wires.size) { "states" }
            val overlay = root.obj("overlay")
            val durations =
                root.obj("animations").mapValues { (name, v) ->
                    val d =
                        v.jsonObject["duration"]
                            ?.jsonPrimitive
                            ?.doubleOrNull
                            ?.toFloat()
                    requireNotNull(d?.takeIf { it > 0f && it.isFinite() }) { "duration of $name" }
                }
            val contract =
                RigContract(
                    idle = perState(root.obj("idle")),
                    longIdle = perState(root.obj("long_idle")),
                    entry = perState(root.obj("entry")),
                    pairs = pairs(root.obj("pair_transitions")),
                    enter = name(overlay.string("enter")),
                    exit = name(overlay.string("exit")),
                    mix = mix(root.obj("mix_seconds")),
                    durations = durations,
                )
            require(durations.keys.containsAll(contract.animationNames)) { "an animation without a duration" }
            return contract
        }

        private fun name(value: String?): String = requireNotNull(value?.takeIf { it.isNotEmpty() }) { "empty name" }

        private fun perState(obj: JsonObject): Map<Expression, String> {
            require(obj.size == Expression.entries.size) { "states" }
            return Expression.entries.associateWith { name(obj.string(it.wire)) }
        }

        private fun pairs(obj: JsonObject): Map<Pair<Expression, Expression>, String> {
            val byWire = Expression.entries.associateBy { it.wire }
            return obj.entries.associate { (key, value) ->
                val parts = key.split('>')
                require(parts.size == 2) { key }
                val from = requireNotNull(byWire[parts[0]]) { key }
                val to = requireNotNull(byWire[parts[1]]) { key }
                require(from != to) { key }
                (from to to) to name((value as? JsonPrimitive)?.contentOrNull)
            }
        }

        private fun mix(obj: JsonObject): Mix {
            fun f(key: String): Float {
                val v = obj[key]?.jsonPrimitive?.doubleOrNull
                return requireNotNull(v?.takeIf { it in MIX_RANGE }) { key }.toFloat()
            }
            return Mix(
                intoEntry = f("into_entry"),
                intoPair = f("into_pair_transition"),
                toIdle = f("transition_to_idle"),
                intoLongIdle = f("into_long_idle"),
                longIdleToIdle = f("long_idle_to_idle"),
                interrupt = f("interrupt"),
            )
        }

        private fun JsonObject.obj(key: String): JsonObject = getValue(key).jsonObject

        private fun JsonObject.string(key: String): String? =
            (
                get(
                    key,
                ) as? JsonPrimitive
            )?.takeIf { it.isString }?.content
    }
}
