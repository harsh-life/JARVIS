package com.hypermind.jarvis

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

/** OD-CHAR-4: the activity feed asks on a cadence and sooner on a nudge, and never fails loudly. */
@OptIn(ExperimentalCoroutinesApi::class)
class CharacterActivityFeedTest {
    @Test
    fun `it asks at once, then every interval, and forwards every answer`() =
        runTest {
            val asked = mutableListOf<Long>()
            val got = mutableListOf<String?>()
            val job =
                launch {
                    CharacterActivityFeed.run(
                        intervalMillis = 15_000,
                        nudges = MutableSharedFlow<Unit>(),
                        clock = { testScheduler.currentTime },
                        fetch = {
                            asked += testScheduler.currentTime
                            "report ${asked.size}"
                        },
                        onReport = { got += it },
                    )
                }
            runCurrent()
            advanceTimeBy(30_001)
            job.cancel()
            assertEquals(listOf(0L, 15_000L, 30_000L), asked)
            assertEquals(listOf("report 1", "report 2", "report 3"), got)
        }

    @Test
    fun `a nudge asks sooner, but never twice within the gap`() =
        runTest {
            val nudges = MutableSharedFlow<Unit>(extraBufferCapacity = 4)
            val asked = mutableListOf<Long>()
            val job =
                launch {
                    CharacterActivityFeed.run(15_000, nudges, { testScheduler.currentTime }, {
                        asked += testScheduler.currentTime
                        null
                    }, {})
                }
            runCurrent()
            advanceTimeBy(500)
            nudges.tryEmit(Unit) // within the gap: waits until 2 000
            runCurrent()
            advanceTimeBy(1_600)
            runCurrent()
            advanceTimeBy(5_000)
            nudges.tryEmit(Unit) // past the gap: at once
            runCurrent()
            job.cancel()
            assertEquals(listOf(0L, 2_000L, 7_100L), asked)
        }

    @Test
    fun `a failing request is forwarded as no answer and the feed carries on`() =
        runTest {
            val got = mutableListOf<String?>()
            var calls = 0
            val job =
                launch {
                    CharacterActivityFeed.run(15_000, MutableSharedFlow<Unit>(), { testScheduler.currentTime }, {
                        calls++
                        if (calls == 1) error("offline") else "ok"
                    }, { got += it })
                }
            runCurrent()
            advanceTimeBy(15_001)
            job.cancel()
            assertEquals(listOf(null, "ok"), got)
        }
}
