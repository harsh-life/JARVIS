package com.hypermind.jarvis.auth

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import com.hypermind.jarvis.contract.DeviceProof
import java.security.GeneralSecurityException
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.ProviderException
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/** The device cannot hold a user-presence-bound key (e.g. no secure lock screen). */
class StepUpUnavailable(
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause)

/**
 * The step-up key (docs/23 §3, 03 §5.5): ECDSA P-256 in the Android Keystore,
 * created so that **every** signature needs the user's biometric or device
 * credential — the platform, not this app, enforces that a person is present.
 * It is invalidated if the biometrics change. Only its public half ever leaves
 * the Keystore.
 */
interface StepUpKeyStore {
    /** Create (or replace) the key; returns its SubjectPublicKeyInfo, base64url. */
    fun create(): String

    /** A signer bound to the key — usable only after the user authenticates. */
    fun signer(): Signature?

    fun destroy()
}

class KeystoreStepUpKeyStore : StepUpKeyStore {
    private fun keyStore() = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }

    override fun create(): String {
        destroy()
        val pair =
            try {
                try {
                    generate(strongBox = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P)
                } catch (ignored: ProviderException) {
                    // StrongBoxUnavailableException (API 28+) is a ProviderException:
                    // fall back to the TEE-backed Keystore.
                    generate(strongBox = false)
                }
            } catch (e: ProviderException) {
                // Typically: no secure lock screen, so no user-bound key can exist.
                throw StepUpUnavailable("no user-bound key", e)
            } catch (e: GeneralSecurityException) {
                throw StepUpUnavailable("no user-bound key", e)
            }
        return DeviceProof.b64(pair.public.encoded)
    }

    private fun generate(strongBox: Boolean) =
        KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE).run {
            val spec =
                KeyGenParameterSpec
                    .Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .setUserAuthenticationRequired(true)
                    .setInvalidatedByBiometricEnrollment(true)
                    .apply {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                            // Every use, biometric or device credential.
                            setUserAuthenticationParameters(
                                0,
                                KeyProperties.AUTH_BIOMETRIC_STRONG or KeyProperties.AUTH_DEVICE_CREDENTIAL,
                            )
                        } else {
                            @Suppress("DEPRECATION")
                            setUserAuthenticationValidityDurationSeconds(-1)
                        }
                        if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) setIsStrongBoxBacked(true)
                    }.build()
            initialize(spec)
            generateKeyPair()
        }

    override fun signer(): Signature? {
        val key = keyStore().getKey(ALIAS, null) as? PrivateKey ?: return null
        return try {
            Signature.getInstance("SHA256withECDSA").apply { initSign(key) }
        } catch (ignored: GeneralSecurityException) {
            // KeyPermanentlyInvalidatedException among them: biometrics changed.
            null
        }
    }

    override fun destroy() {
        keyStore().run { if (containsAlias(ALIAS)) deleteEntry(ALIAS) }
    }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "jarvis_step_up"
    }
}
