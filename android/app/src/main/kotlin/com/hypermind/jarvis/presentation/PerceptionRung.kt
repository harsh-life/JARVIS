package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.contract.PerceptionLevel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Which rung of the perception ladder the current screen read is on
 * (docs/23 §6), for the status display only. It holds a level and nothing
 * else — never what was read — and is only consulted while a read operation
 * is running (see [Presenter.activity]).
 */
object PerceptionRung {
    private val _level = MutableStateFlow<PerceptionLevel?>(null)
    val level: StateFlow<PerceptionLevel?> = _level.asStateFlow()

    fun report(level: PerceptionLevel) {
        _level.value = level
    }
}
