package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.channel.RunningOperation
import com.hypermind.jarvis.contract.PerceptionLevel
import com.hypermind.jarvis.contract.PlatformDependency
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

/** The live stream follows every canonical input through the one mapping. */
class PresentationStreamTest {
    private val channel = MutableStateFlow<ChannelState>(ChannelState.Stopped)
    private val task = MutableStateFlow<TaskSnapshot>(TaskSnapshot.Idle)
    private val ops = MutableStateFlow<List<RunningOperation>>(emptyList())
    private val rung = MutableStateFlow<PerceptionLevel?>(null)
    private val enrolled = MutableStateFlow(true)
    private val platforms = MutableStateFlow<Map<PlatformDependency, Boolean>>(emptyMap())
    private val push = MutableStateFlow(PushStatus.NOT_OFFERED)
    private val voice = MutableStateFlow(VoiceActivity.NONE)
    private val stream = PresentationStream.of(channel, task, ops, rung, enrolled, platforms, push, voice)

    private fun now() = runBlocking { stream.first() }

    @Test
    fun `the stream follows the channel, the task, what the phone is doing, and push`() {
        assertEquals(TaskPhase.OFFLINE, now().taskStatus)
        channel.value = ChannelState.Connected(Instant.parse("2099-01-01T00:00:00Z"))
        assertEquals(TaskPhase.IDLE, now().taskStatus)
        task.value = TaskSnapshot.InFlight
        assertEquals(TaskPhase.THINKING, now().taskStatus)
        ops.value = listOf(RunningOperation("t", "device.read", "read_screen", "accessibility.read_tree"))
        rung.value = PerceptionLevel.OCR
        assertEquals(TaskPhase.EXECUTING, now().taskStatus)
        assertEquals(DeviceActivity.USING_OCR, now().deviceContext.activity)
        ops.value = emptyList()
        assertEquals(TaskPhase.THINKING, now().taskStatus) // the rung alone means nothing
        push.value = PushStatus.ON
        platforms.value = mapOf(PlatformDependency.SHIZUKU to false)
        assertEquals(PushWake.ON, now().deviceContext.pushWake)
        assertEquals(false, now().deviceContext.platforms[PlatformDependency.SHIZUKU])
        enrolled.value = false
        task.value = TaskSnapshot.Idle
        channel.value = ChannelState.Stopped
        assertEquals(ConnectionPhase.NOT_ENROLLED, now().deviceContext.connection)
    }

    @Test
    fun `push-to-talk shows as listening only while nothing else is happening`() {
        channel.value = ChannelState.Connected(Instant.parse("2099-01-01T00:00:00Z"))
        voice.value = VoiceActivity.LISTENING
        assertEquals(TaskPhase.LISTENING, now().taskStatus)
        assertEquals(VoiceActivity.LISTENING, now().deviceContext.voice)
        // A task's own phase outranks the microphone: voice is display only.
        task.value = TaskSnapshot.InFlight
        assertEquals(TaskPhase.THINKING, now().taskStatus)
        task.value = TaskSnapshot.Idle
        voice.value = VoiceActivity.NONE
        assertEquals(TaskPhase.IDLE, now().taskStatus)
    }
}
