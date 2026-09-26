package com.hypermind.jarvis.channel

import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.ResultEnvelope

/**
 * Turns one server operation envelope into one result (docs/23 §4). The
 * channel runs each in its own coroutine; cancelling that coroutine is how a
 * `cancel` frame aborts it, and a cancelled operation's result is never sent.
 */
fun interface OperationHandler {
    suspend fun handle(envelope: OperationEnvelope): ResultEnvelope
}

/**
 * The handler until the device-side guard and primitives exist: every
 * operation fails explicitly — never silently, never "done".
 */
object UnimplementedOperations : OperationHandler {
    override suspend fun handle(envelope: OperationEnvelope): ResultEnvelope =
        ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
}
