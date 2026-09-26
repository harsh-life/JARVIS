package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.security.MessageDigest

/**
 * Byte-exact reproduction of the server's `canonical_json` —
 * `json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
 * Used only to recompute the mapping digest, so it accepts exactly what the
 * mapping contains (objects, arrays, strings, integers, booleans, null) and
 * rejects anything whose Python rendering it cannot guarantee (floats).
 */
object CanonicalJson {
    fun encode(element: JsonElement): String = StringBuilder().also { write(element, it) }.toString()

    fun sha256Hex(text: String): String =
        MessageDigest
            .getInstance("SHA-256")
            .digest(text.toByteArray(Charsets.US_ASCII))
            .joinToString("") { "%02x".format(it) }

    private val INTEGER = Regex("-?(0|[1-9][0-9]*)")

    private fun write(
        element: JsonElement,
        out: StringBuilder,
    ) {
        when (element) {
            is JsonNull -> out.append("null")
            is JsonObject -> {
                out.append('{')
                element.keys.sorted().forEachIndexed { i, key ->
                    if (i > 0) out.append(',')
                    writeString(key, out)
                    out.append(':')
                    write(element.getValue(key), out)
                }
                out.append('}')
            }
            is JsonArray -> {
                out.append('[')
                element.forEachIndexed { i, item ->
                    if (i > 0) out.append(',')
                    write(item, out)
                }
                out.append(']')
            }
            is JsonPrimitive ->
                when {
                    element.isString -> writeString(element.content, out)
                    element.content == "true" || element.content == "false" -> out.append(element.content)
                    INTEGER.matches(element.content) -> out.append(element.content)
                    else -> throw IllegalArgumentException("non-integer number in canonical JSON")
                }
        }
    }

    private fun writeString(
        value: String,
        out: StringBuilder,
    ) {
        out.append('"')
        for (ch in value) {
            when {
                ch == '"' -> out.append("\\\"")
                ch == '\\' -> out.append("\\\\")
                ch == '\n' -> out.append("\\n")
                ch == '\r' -> out.append("\\r")
                ch == '\t' -> out.append("\\t")
                ch == '\b' -> out.append("\\b")
                ch == '\u000c' -> out.append("\\f")
                ch.code < 0x20 || ch.code > 0x7e -> out.append("\\u%04x".format(ch.code))
                else -> out.append(ch)
            }
        }
        out.append('"')
    }
}
