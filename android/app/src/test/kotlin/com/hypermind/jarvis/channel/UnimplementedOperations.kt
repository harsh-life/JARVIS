package com.hypermind.jarvis.channel

import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.ResultEnvelope

/** A handler that fails everything explicitly — for channel tests. */
object UnimplementedOperations : OperationHandler {
    override suspend fun handle(envelope: OperationEnvelope): ResultEnvelope =
        ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
}
