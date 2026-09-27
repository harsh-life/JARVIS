package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.DeviceProof
import com.hypermind.jarvis.contract.StepUp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.security.GeneralSecurityException
import java.security.Signature
import java.time.Instant
import java.util.UUID

/** Unlock a step-up signer by the user's biometric or device credential; null if they did not. */
fun interface UserPresence {
    suspend fun authorize(signer: Signature): Signature?
}

sealed interface StepUpResult {
    data class Attested(
        val until: Instant,
    ) : StepUpResult

    /** No usable key (never enrolled, lock screen removed, biometrics changed): enroll again. */
    data object NoKey : StepUpResult

    data object Cancelled : StepUpResult

    data object Failed : StepUpResult
}

/** What the confirmation flow needs from step-up. */
fun interface Reattestation {
    suspend fun reattest(presence: UserPresence): StepUpResult
}

/**
 * docs/23 §3 step-up, device side: a server challenge, signed by the step-up
 * key only after the user authenticates, sent back so the server marks this
 * device re-attested for a few minutes. Needed before approving a
 * `high_irreversible` action. Never a spoken phrase (27 §3), and never
 * satisfied by the background token refresh.
 */

class StepUpFlow(
    private val api: () -> ApiClient,
    private val accessToken: () -> String,
    private val keys: StepUpKeyStore,
    private val deviceId: () -> UUID?,
) : Reattestation {
    /** At enrollment, right after the interactive login. False if the device cannot hold the key. */
    fun enroll(): Boolean =
        try {
            val publicKey = keys.create()
            api().registerStepUpKey(accessToken(), publicKey)
            true
        } catch (ignored: StepUpUnavailable) {
            false
        } catch (ignored: IOException) {
            keys.destroy()
            false
        }

    override suspend fun reattest(presence: UserPresence): StepUpResult {
        val device = deviceId() ?: return StepUpResult.NoKey
        val signer = keys.signer() ?: return StepUpResult.NoKey
        val (token, challenge) =
            try {
                withContext(Dispatchers.IO) {
                    val token = accessToken()
                    token to api().stepUpChallenge(token)
                }
            } catch (ignored: IOException) {
                return StepUpResult.Failed
            }
        val unlocked = presence.authorize(signer) ?: return StepUpResult.Cancelled
        val signature =
            try {
                unlocked.run {
                    update(StepUp.message(device.toString(), challenge))
                    sign()
                }
            } catch (ignored: GeneralSecurityException) {
                return StepUpResult.Failed
            }
        return try {
            withContext(Dispatchers.IO) {
                StepUpResult.Attested(api().stepUpAttest(token, challenge, DeviceProof.b64(signature)))
            }
        } catch (ignored: IOException) {
            StepUpResult.Failed
        }
    }
}
