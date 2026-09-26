package com.hypermind.jarvis.auth

import com.google.crypto.tink.subtle.Ed25519Sign
import com.google.crypto.tink.subtle.Ed25519Verify

/** A software key standing in for the Keystore in JVM tests. */
class TestKey : DeviceKey {
    private val pair = Ed25519Sign.KeyPair.newKeyPair()
    override val publicKey: ByteArray = pair.publicKey
    override val protection = KeyProtection.SEALED_SOFTWARE

    override fun sign(message: ByteArray): ByteArray = Ed25519Sign(pair.privateKey).sign(message)

    fun verify(
        signature: ByteArray,
        message: ByteArray,
    ) = Ed25519Verify(publicKey).verify(signature, message)
}

class TestKeyStore : DeviceKeyStore {
    var key: TestKey? = null
    var destroyed = 0

    override fun load(): DeviceKey? = key

    override fun create(): DeviceKey = TestKey().also { key = it }

    override fun destroy() {
        destroyed += 1
        key = null
    }
}
