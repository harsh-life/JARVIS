package com.hypermind.jarvis.contract

/**
 * Unpadded base64url (RFC 4648 §5) — the encoding every server credential
 * format uses. Written out rather than taken from `java.util.Base64`, which
 * Android only has from API 26 (this client supports 24).
 */
object Base64Url {
    private val ALPHABET: String = (('A'..'Z') + ('a'..'z') + ('0'..'9') + '-' + '_').joinToString("")
    private val INDEX = IntArray(128) { -1 }.also { table -> ALPHABET.forEachIndexed { i, c -> table[c.code] = i } }

    fun encode(data: ByteArray): String {
        val out = StringBuilder((data.size * 4 + 2) / 3)
        var i = 0
        while (i + 2 < data.size) {
            val n =
                (data[i].toInt() and 0xff shl 16) or (data[i + 1].toInt() and 0xff shl 8) or
                    (data[i + 2].toInt() and 0xff)
            out.append(ALPHABET[n shr 18 and 63]).append(ALPHABET[n shr 12 and 63])
            out.append(ALPHABET[n shr 6 and 63]).append(ALPHABET[n and 63])
            i += 3
        }
        val rest = data.size - i
        if (rest > 0) {
            val n = (data[i].toInt() and 0xff shl 16) or (if (rest == 2) data[i + 1].toInt() and 0xff shl 8 else 0)
            out.append(ALPHABET[n shr 18 and 63]).append(ALPHABET[n shr 12 and 63])
            if (rest == 2) out.append(ALPHABET[n shr 6 and 63])
        }
        return out.toString()
    }

    /** Decodes unpadded (or padded) base64url; throws [IllegalArgumentException] on anything else. */
    fun decode(text: String): ByteArray {
        val body = text.trimEnd('=')
        require(body.length % 4 != 1) { "invalid base64url length" }
        val out = java.io.ByteArrayOutputStream(body.length * 3 / 4)
        var buffer = 0
        var bits = 0
        for (c in body) {
            val v = if (c.code < 128) INDEX[c.code] else -1
            require(v >= 0) { "invalid base64url character" }
            buffer = (buffer shl 6) or v
            bits += 6
            if (bits >= 8) {
                bits -= 8
                out.write(buffer shr bits and 0xff)
            }
        }
        return out.toByteArray()
    }
}
