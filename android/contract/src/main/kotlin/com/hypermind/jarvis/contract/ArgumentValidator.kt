package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * docs/23 §5.2 step 4: the arguments are well formed for the primitive.
 * The same rules as the server's `argument_problem`
 * (`server/execution/android.py`); the shared conformance vectors hold the two
 * to identical verdicts. Unknown keys are an error, never ignored (AND-T6).
 */
object ArgumentValidator {
    private val INTEGER = Regex("-?(0|[1-9][0-9]*)")

    /** Why [arguments] is malformed for [spec], or null when well formed. */
    fun problem(
        spec: PrimitiveSpec,
        arguments: JsonObject,
    ): String? {
        val unknown = arguments.keys - spec.arguments.keys
        if (unknown.isNotEmpty()) return "unknown argument(s) ${unknown.sorted()}"
        val perArgument =
            spec.arguments.entries.firstNotNullOfOrNull { (name, arg) ->
                val raw = arguments[name]
                when {
                    raw != null -> valueProblem(name, arg, raw)
                    arg.required -> "missing required argument '$name'"
                    else -> null
                }
            }
        return perArgument ?: oneOfProblem(spec, arguments)
    }

    private fun valueProblem(
        name: String,
        arg: ArgumentSpec,
        raw: JsonElement,
    ): String? {
        val value = raw as? JsonPrimitive ?: return "'$name' must be a scalar"
        return when (arg.kind) {
            ArgumentKind.STRING, ArgumentKind.ENUM -> stringProblem(name, arg, value)
            ArgumentKind.INT -> intProblem(name, arg, value)
            ArgumentKind.BOOL ->
                if (value.isString || (value.content != "true" && value.content != "false")) {
                    "'$name' must be a boolean"
                } else {
                    null
                }
        }
    }

    private fun stringProblem(
        name: String,
        arg: ArgumentSpec,
        value: JsonPrimitive,
    ): String? {
        val length = value.content.codePointCount(0, value.content.length)
        return when {
            !value.isString || value.content.isEmpty() -> "'$name' must be a non-empty string"
            arg.maxLength != null && length > arg.maxLength -> "'$name' exceeds ${arg.maxLength}"
            arg.kind == ArgumentKind.ENUM && value.content !in arg.values -> "'$name' not allowed"
            else -> null
        }
    }

    private fun intProblem(
        name: String,
        arg: ArgumentSpec,
        value: JsonPrimitive,
    ): String? {
        if (value.isString || !INTEGER.matches(value.content)) return "'$name' must be an integer"
        val number = value.content.toBigInteger()
        return when {
            arg.minimum != null && number < arg.minimum.toBigInteger() -> "'$name' below minimum"
            arg.maximum != null && number > arg.maximum.toBigInteger() -> "'$name' above maximum"
            else -> null
        }
    }

    private fun oneOfProblem(
        spec: PrimitiveSpec,
        arguments: JsonObject,
    ): String? {
        if (spec.oneOf.isEmpty()) return null
        val present = spec.oneOf.flatten().toSet() intersect arguments.keys
        val matches = spec.oneOf.count { it.toSet() == present }
        return if (matches == 1) null else "exactly one target group must be given"
    }
}
