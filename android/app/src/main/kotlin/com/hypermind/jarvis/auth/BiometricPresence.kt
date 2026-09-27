package com.hypermind.jarvis.auth

import android.os.Build
import androidx.biometric.BiometricManager.Authenticators
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlinx.coroutines.suspendCancellableCoroutine
import java.security.Signature
import kotlin.coroutines.resume

/**
 * [UserPresence] by the platform's own prompt: the signer is bound to the
 * authentication (a `CryptoObject`), so the Keystore releases the key for
 * exactly this one signature and only after the user's biometric (or, on
 * Android 11+, device credential). This app never sees the fingerprint or PIN.
 */
class BiometricPresence(
    private val activity: FragmentActivity,
    private val title: String,
    private val subtitle: String,
    private val cancel: String,
) : UserPresence {
    override suspend fun authorize(signer: Signature): Signature? =
        suspendCancellableCoroutine { cont ->
            val prompt =
                BiometricPrompt(
                    activity,
                    ContextCompat.getMainExecutor(activity),
                    object : BiometricPrompt.AuthenticationCallback() {
                        override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                            if (cont.isActive) cont.resume(result.cryptoObject?.signature)
                        }

                        override fun onAuthenticationError(
                            errorCode: Int,
                            errString: CharSequence,
                        ) {
                            if (cont.isActive) cont.resume(null)
                        }
                    },
                )
            val info =
                BiometricPrompt.PromptInfo
                    .Builder()
                    .setTitle(title)
                    .setSubtitle(subtitle)
                    .apply {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                            setAllowedAuthenticators(
                                Authenticators.BIOMETRIC_STRONG or Authenticators.DEVICE_CREDENTIAL,
                            )
                        } else {
                            setAllowedAuthenticators(Authenticators.BIOMETRIC_STRONG)
                            setNegativeButtonText(cancel)
                        }
                    }.build()
            prompt.authenticate(info, BiometricPrompt.CryptoObject(signer))
            cont.invokeOnCancellation { prompt.cancelAuthentication() }
        }
}
