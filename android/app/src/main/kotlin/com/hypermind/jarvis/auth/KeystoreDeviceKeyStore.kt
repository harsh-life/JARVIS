package com.hypermind.jarvis.auth

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import androidx.annotation.RequiresApi
import com.google.crypto.tink.subtle.Ed25519Sign
import java.io.File
import java.security.GeneralSecurityException
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.ProviderException
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The production [DeviceKeyStore].
 *
 * Preferred: an Ed25519 key generated **inside** the Android Keystore
 * (KeyMint, Android 13+) — the private key is non-exportable and, on most
 * devices, lives in the TEE or StrongBox. Where the device's Keystore cannot
 * hold Ed25519, the fallback generates the key in memory and immediately seals
 * it with an AES-256-GCM key that *is* in the Keystore (hardware-backed where
 * available); the plaintext seed exists only for the duration of one signature.
 * The protection actually obtained is reported (docs/23 §3 "hardware-backed
 * where available" — not assumed).
 */
class KeystoreDeviceKeyStore(
    private val context: Context,
) : DeviceKeyStore {
    private val keyStore: KeyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
    private val sealedFile: File get() = File(context.noBackupFilesDir, SEALED_FILE)

    override fun load(): DeviceKey? =
        when {
            keyStore.containsAlias(SIGNING_ALIAS) && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU ->
                keystoreKey()
            sealedFile.exists() && keyStore.containsAlias(SEAL_ALIAS) -> sealedKey()
            else -> null
        }

    override fun create(): DeviceKey {
        destroy()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            try {
                generateKeystoreEd25519()
                return keystoreKey()
            } catch (ignored: GeneralSecurityException) {
                keyStore.deleteEntry(SIGNING_ALIAS)
            } catch (ignored: ProviderException) {
                // KeyMint without Curve25519 support.
                keyStore.deleteEntry(SIGNING_ALIAS)
            } catch (ignored: IllegalArgumentException) {
                keyStore.deleteEntry(SIGNING_ALIAS)
            }
        }
        return createSealed()
    }

    override fun destroy() {
        if (keyStore.containsAlias(SIGNING_ALIAS)) keyStore.deleteEntry(SIGNING_ALIAS)
        if (keyStore.containsAlias(SEAL_ALIAS)) keyStore.deleteEntry(SEAL_ALIAS)
        sealedFile.delete()
    }

    // ── Keystore-native Ed25519 ─────────────────────────────────────────

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private fun generateKeystoreEd25519() {
        val spec =
            KeyGenParameterSpec
                .Builder(SIGNING_ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(ECGenParameterSpec("ed25519"))
                .setDigests(KeyProperties.DIGEST_NONE)
                .build()
        KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE).run {
            initialize(spec)
            generateKeyPair()
        }
    }

    private fun keystoreKey(): DeviceKey {
        val privateKey = keyStore.getKey(SIGNING_ALIAS, null) as PrivateKey
        val encoded = keyStore.getCertificate(SIGNING_ALIAS).publicKey.encoded
        val publicKey = Ed25519Spki.rawPublicKey(encoded)
        val hardware = isHardwareBacked(privateKey)
        return object : DeviceKey {
            override val publicKey = publicKey
            override val protection = if (hardware) KeyProtection.KEYSTORE_HARDWARE else KeyProtection.KEYSTORE_SOFTWARE

            override fun sign(message: ByteArray): ByteArray =
                Signature.getInstance("Ed25519").run {
                    initSign(privateKey)
                    update(message)
                    sign()
                }
        }
    }

    // ── sealed fallback ─────────────────────────────────────────────────

    private fun createSealed(): DeviceKey {
        val sealingKey = generateSealingKey()
        val pair = Ed25519Sign.KeyPair.newKeyPair()
        val seed = pair.privateKey
        try {
            val cipher = Cipher.getInstance(AES_GCM).apply { init(Cipher.ENCRYPT_MODE, sealingKey) }
            val sealed = cipher.iv + cipher.doFinal(seed)
            sealedFile.writeBytes(byteArrayOf(pair.publicKey.size.toByte()) + pair.publicKey + sealed)
        } finally {
            seed.fill(0)
        }
        return sealedKey()
    }

    private fun sealedKey(): DeviceKey {
        val bytes = sealedFile.readBytes()
        val publicSize = bytes[0].toInt()
        val publicKey = bytes.copyOfRange(1, 1 + publicSize)
        val iv = bytes.copyOfRange(1 + publicSize, 1 + publicSize + GCM_IV_BYTES)
        val ciphertext = bytes.copyOfRange(1 + publicSize + GCM_IV_BYTES, bytes.size)
        val sealingKey = keyStore.getKey(SEAL_ALIAS, null) as SecretKey
        val hardware = isHardwareBacked(sealingKey)
        return object : DeviceKey {
            override val publicKey = publicKey
            override val protection = if (hardware) KeyProtection.SEALED_HARDWARE else KeyProtection.SEALED_SOFTWARE

            override fun sign(message: ByteArray): ByteArray {
                val cipher =
                    Cipher.getInstance(AES_GCM).apply {
                        init(Cipher.DECRYPT_MODE, sealingKey, GCMParameterSpec(GCM_TAG_BITS, iv))
                    }
                val seed = cipher.doFinal(ciphertext)
                try {
                    return Ed25519Sign(seed).sign(message)
                } finally {
                    seed.fill(0)
                }
            }
        }
    }

    private fun generateSealingKey(): SecretKey {
        fun spec(strongBox: Boolean) =
            KeyGenParameterSpec
                .Builder(SEAL_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(AES_KEY_BITS)
                .apply { if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) setIsStrongBoxBacked(true) }
                .build()
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        return try {
            generator.init(spec(strongBox = true))
            generator.generateKey()
        } catch (ignored: GeneralSecurityException) {
            generator.init(spec(strongBox = false))
            generator.generateKey()
        } catch (ignored: ProviderException) {
            generator.init(spec(strongBox = false))
            generator.generateKey()
        }
    }

    private fun isHardwareBacked(key: java.security.Key): Boolean =
        try {
            val info =
                if (key is SecretKey) {
                    javax.crypto.SecretKeyFactory
                        .getInstance(key.algorithm, ANDROID_KEYSTORE)
                        .getKeySpec(key, KeyInfo::class.java) as KeyInfo
                } else {
                    KeyFactory.getInstance(key.algorithm, ANDROID_KEYSTORE).getKeySpec(key, KeyInfo::class.java)
                }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                info.securityLevel == KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT ||
                    info.securityLevel == KeyProperties.SECURITY_LEVEL_STRONGBOX
            } else {
                @Suppress("DEPRECATION")
                info.isInsideSecureHardware
            }
        } catch (ignored: GeneralSecurityException) {
            false
        }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val SIGNING_ALIAS = "jarvis-device-ed25519"
        const val SEAL_ALIAS = "jarvis-device-seal"
        const val SEALED_FILE = "device_key.sealed"
        const val AES_GCM = "AES/GCM/NoPadding"
        const val AES_KEY_BITS = 256
        const val GCM_IV_BYTES = 12
        const val GCM_TAG_BITS = 128
    }
}

/** Ed25519 SubjectPublicKeyInfo (RFC 8410) → the raw 32-byte key the server stores. */
object Ed25519Spki {
    private val PREFIX = byteArrayOf(0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00)

    fun rawPublicKey(encoded: ByteArray): ByteArray {
        require(encoded.size == PREFIX.size + 32 && encoded.copyOfRange(0, PREFIX.size).contentEquals(PREFIX)) {
            "not an Ed25519 public key"
        }
        return encoded.copyOfRange(PREFIX.size, encoded.size)
    }
}
