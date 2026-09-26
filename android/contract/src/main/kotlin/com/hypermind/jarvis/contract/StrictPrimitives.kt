package com.hypermind.jarvis.contract

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull

/**
 * kotlinx.serialization reads a quoted `"81"` as the number 81 even when not
 * lenient; the server's strict models refuse it. These serializers make the
 * client refuse it too, so both sides accept exactly the same results (the
 * shared perception samples hold them to it).
 */
private fun strictPrimitive(decoder: Decoder): JsonPrimitive {
    val json = decoder as? JsonDecoder ?: throw SerializationException("JSON only")
    val element = json.decodeJsonElement()
    if (element !is JsonPrimitive || element.isString) throw SerializationException("expected a bare JSON literal")
    return element
}

object StrictIntSerializer : KSerializer<Int> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor("StrictInt", PrimitiveKind.INT)

    override fun deserialize(decoder: Decoder): Int {
        val primitive = strictPrimitive(decoder)
        // An integer literal only: 81.5 or 81.0 is not an int.
        if (!Regex("-?\\d+").matches(primitive.content)) throw SerializationException("expected an integer")
        return primitive.intOrNull ?: throw SerializationException("integer out of range")
    }

    override fun serialize(
        encoder: Encoder,
        value: Int,
    ) = encoder.encodeInt(value)
}

object StrictBooleanSerializer : KSerializer<Boolean> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor("StrictBoolean", PrimitiveKind.BOOLEAN)

    override fun deserialize(decoder: Decoder): Boolean =
        strictPrimitive(decoder).booleanOrNull ?: throw SerializationException("expected a boolean")

    override fun serialize(
        encoder: Encoder,
        value: Boolean,
    ) = encoder.encodeBoolean(value)
}

object StrictDoubleSerializer : KSerializer<Double> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor("StrictDouble", PrimitiveKind.DOUBLE)

    override fun deserialize(decoder: Decoder): Double =
        strictPrimitive(decoder).doubleOrNull ?: throw SerializationException("expected a number")

    override fun serialize(
        encoder: Encoder,
        value: Double,
    ) = encoder.encodeDouble(value)
}
