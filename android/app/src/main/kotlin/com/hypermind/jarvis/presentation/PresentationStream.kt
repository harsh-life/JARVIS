package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.channel.RunningOperation
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PlatformDependency
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine

/**
 * The live [PresentationState]: the canonical inputs, combined, through the
 * one mapping ([Presenter]). Every surface collects this one flow.
 */
object PresentationStream {
    @Suppress("LongParameterList")
    fun of(
        channel: Flow<ChannelState>,
        task: Flow<TaskSnapshot>,
        operations: Flow<List<RunningOperation>>,
        rung: Flow<PerceptionLevel?>,
        enrolled: Flow<Boolean>,
        platforms: Flow<Map<PlatformDependency, Boolean>>,
        push: Flow<PushStatus>,
    ): Flow<PresentationState> =
        combine(
            combine(channel, task, operations, rung) { c, t, ops, level ->
                PresentationInputs(
                    enrolled = true,
                    channel = c,
                    task = t,
                    activeOperations =
                        ops.map {
                            ActiveOperation(
                                it.taskId,
                                it.capability,
                                it.operation,
                                it.primitive,
                            )
                        },
                    perceptionLevel = level,
                )
            },
            enrolled,
            platforms,
            push,
        ) { inputs, isEnrolled, platformMap, pushNow ->
            Presenter.of(inputs.copy(enrolled = isEnrolled, platforms = platformMap, push = pushNow))
        }
}
