package com.hypermind.jarvis.action

import com.hypermind.jarvis.channel.OperationHandler
import com.hypermind.jarvis.contract.DeviceGuard
import com.hypermind.jarvis.contract.DeviceLocalState
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.GuardVerdict
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PerceptionCheck
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ResultStatus
import com.hypermind.jarvis.contract.encode

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
 *
 * Two more narrowing steps, both refusal-only:
 * * **Dependencies, checked now.** A primitive whose on-device dependency is
 *   missing at this moment (docs/23 §5.3 — e.g. Shizuku after a reboot) is
 *   refused as `platform_unavailable` naming it. It is not queued, retried or
 *   run by some other mechanism.
 * * **The result checks itself before it leaves.** An `ok` result must match
 *   its declared kind, the rung rules and the operation's package, and fit
 *   the primitive's size bound ([PerceptionCheck]); one that does not is
 *   replaced by a failure and never sent.
 */
class GuardedOperations(
    private val guard: () -> DeviceGuard?,
    private val localState: () -> DeviceLocalState,
    private val primitives: Map<String, Primitive>,
    private val available: (PlatformDependency) -> Boolean,
) : OperationHandler {
    override suspend fun handle(envelope: OperationEnvelope): ResultEnvelope {
        val guard = guard() ?: return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        return when (val verdict = guard.check(envelope, localState())) {
            is GuardVerdict.Refused -> ResultEnvelope.refused(envelope.opId, verdict.reason)
            is GuardVerdict.Allowed -> {
                val spec = verdict.spec
                val primitive =
                    primitives[spec.primitive]
                        // In the table but not built into this client: an
                        // explicit failure, never a silent success.
                        ?: return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
                spec.dependencies.firstOrNull { !available(it) }?.let {
                    return ResultEnvelope.refused(envelope.opId, RefusalReason.PLATFORM_UNAVAILABLE, it)
                }
                checked(envelope, spec, primitive.run(envelope, spec))
            }
        }
    }

    private fun checked(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
        result: ResultEnvelope,
    ): ResultEnvelope {
        if (result.opId != envelope.opId) return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        if (result.status != ResultStatus.OK) return result
        val body = result.result ?: return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        val problem = PerceptionCheck.problem(spec.result, body, result.perceptionLevel, envelope.packageName)
        val size = result.encode().toByteArray(Charsets.UTF_8).size
        return if (problem != null || size > spec.maxResultBytes) {
            ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        } else {
            result
        }
    }
}
