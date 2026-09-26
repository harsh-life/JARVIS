package com.hypermind.jarvis.action

import com.hypermind.jarvis.channel.OperationHandler
import com.hypermind.jarvis.contract.DeviceGuard
import com.hypermind.jarvis.contract.DeviceLocalState
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.GuardVerdict
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.ResultEnvelope

/** One executable primitive (Accessibility action, Android API, typed Shizuku call). */
fun interface Primitive {
    suspend fun run(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope
}

/**
 * Every operation passes the device-side guard (docs/23 §5.2) before anything
 * runs; a refusal is reported with its reason and nothing executes. An allowed
 * operation runs its registered primitive — and only that one: the primitive is
 * chosen by the mapping triple the guard already verified, never by anything
 * else in the envelope.
 */
class GuardedOperations(
    private val guard: () -> DeviceGuard?,
    private val localState: () -> DeviceLocalState,
    private val primitives: Map<String, Primitive>,
) : OperationHandler {
    override suspend fun handle(envelope: OperationEnvelope): ResultEnvelope {
        val guard = guard() ?: return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        return when (val verdict = guard.check(envelope, localState())) {
            is GuardVerdict.Refused -> ResultEnvelope.refused(envelope.opId, verdict.reason)
            is GuardVerdict.Allowed -> {
                val primitive =
                    primitives[verdict.spec.primitive]
                        // In the table but not built into this client: an
                        // explicit failure, never a silent success.
                        ?: return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
                primitive.run(envelope, verdict.spec)
            }
        }
    }
}
