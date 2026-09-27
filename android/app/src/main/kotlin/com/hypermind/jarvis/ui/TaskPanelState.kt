package com.hypermind.jarvis.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.auth.StepUpResult
import com.hypermind.jarvis.contract.ConfirmationText
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.tasks.TaskController
import com.hypermind.jarvis.voice.VoiceInputState
import com.hypermind.jarvis.voice.VoiceMessages
import com.hypermind.jarvis.voice.VoiceRouting

/** What the task panel is showing. */
sealed interface TaskPanelState {
    data object Idle : TaskPanelState

    data object Working : TaskPanelState

    data class Answer(
        val text: String,
    ) : TaskPanelState

    data class Confirm(
        val taskId: String,
        val pending: PendingAction,
    ) : TaskPanelState

    data class Message(
        val text: String,
    ) : TaskPanelState

    companion object {
        fun of(outcome: TaskController.Outcome): TaskPanelState =
            when (outcome) {
                is TaskController.Outcome.Unreachable -> Message("Could not reach your server.")
                is TaskController.Outcome.StepUpBlocked ->
                    Message(
                        when (outcome.result) {
                            StepUpResult.NoKey ->
                                "This phone cannot verify you (set a screen lock, then sign in again). " +
                                    "Nothing was approved."
                            StepUpResult.Cancelled -> "Not verified. Nothing was approved; the action is still waiting."
                            else -> "Verification failed. Nothing was approved."
                        },
                    )
                is TaskController.Outcome.Shown -> of(outcome.view)
            }

        private fun of(view: TaskView): TaskPanelState =
            when (view) {
                is TaskView.NeedsConfirmation -> Confirm(view.taskId, view.pending)
                is TaskView.Failed -> Message(view.message.ifBlank { view.code })
                is TaskView.Result ->
                    when (view.result.status) {
                        TaskStatus.COMPLETED -> Answer(view.result.response.orEmpty())
                        TaskStatus.WAITING_FOR_PLATFORM -> {
                            val what = view.result.waitingFor?.dependency
                            if (what == "device_channel") {
                                // docs/23 §4: the device was offline; it was asked to reconnect.
                                Message(
                                    "Waiting for this phone to reconnect to JARVIS. The task continues once it does.",
                                )
                            } else {
                                val setting = what ?: "a phone setting"
                                Message("Waiting for $setting to be turned on. The task continues once it is.")
                            }
                        }
                        TaskStatus.CANCELLED -> Message("Cancelled.")
                        TaskStatus.FAILED -> Message(view.result.failure?.message ?: "The task failed.")
                        TaskStatus.RUNNING, TaskStatus.AWAITING_CONFIRMATION -> Message("Still working…")
                    }
            }
    }
}

@Composable
fun TaskPanel(
    state: TaskPanelState,
    onSubmit: (String) -> Unit,
    onApprove: (String, PendingAction) -> Unit,
    onDecline: (String, PendingAction) -> Unit,
    draft: String? = null,
    voice: VoiceControls? = null,
) {
    // A draft (a reminder's words, or a transcript) only fills the box; the
    // user still presses Send, and it goes out as an ordinary task.
    var input by remember(draft) { mutableStateOf(draft.orEmpty()) }
    // docs/27 §3: a transcript joins the draft — the only thing it can do. It
    // never reaches the confirmation card below.
    val heard = voice?.state as? VoiceInputState.Heard
    LaunchedEffect(heard) {
        if (heard != null) input = VoiceRouting.route(heard.transcript, input).text
    }
    OutlinedTextField(
        value = input,
        onValueChange = { input = it },
        label = { Text("Ask JARVIS") },
        modifier = Modifier.fillMaxWidth(),
    )
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Button(enabled = input.isNotBlank() && state != TaskPanelState.Working, onClick = { onSubmit(input) }) {
            Text("Send")
        }
        voice?.let { VoiceButton(it) }
    }
    voice?.let { VoiceStatus(it.state) }
    when (state) {
        TaskPanelState.Idle -> Unit
        TaskPanelState.Working -> Text("Working…")
        is TaskPanelState.Answer -> Text(state.text, style = MaterialTheme.typography.bodyLarge)
        is TaskPanelState.Message -> Text(state.text, style = MaterialTheme.typography.bodyMedium)
        is TaskPanelState.Confirm ->
            ConfirmationCard(state.pending, { onApprove(state.taskId, state.pending) }) {
                onDecline(state.taskId, state.pending)
            }
    }
}

/** Push-to-talk for the task box (docs/27). `null` when voice input is off. */
data class VoiceControls(
    val state: VoiceInputState,
    val onListen: () -> Unit,
    val onStop: () -> Unit,
)

@Composable
private fun VoiceButton(voice: VoiceControls) {
    when (voice.state) {
        VoiceInputState.Listening -> OutlinedButton(onClick = voice.onStop) { Text("Stop") }
        VoiceInputState.Processing -> OutlinedButton(enabled = false, onClick = {}) { Text("…") }
        else -> OutlinedButton(onClick = voice.onListen) { Text("Speak") }
    }
}

@Composable
private fun VoiceStatus(state: VoiceInputState) {
    val text =
        when (state) {
            VoiceInputState.Listening -> "Listening — tap Stop when you're done."
            VoiceInputState.Processing -> "Recognizing on this phone…"
            is VoiceInputState.Failed -> VoiceMessages.of(state.error)
            else -> null
        }
    text?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
}

/**
 * docs/23 §5.4 (ANDC-T10): every word here comes from [ConfirmationText], i.e.
 * from the server's canonical pending action — never from what the model said.
 */
@Composable
fun ConfirmationCard(
    pending: PendingAction,
    onApprove: () -> Unit,
    onDecline: () -> Unit,
) {
    val screen = ConfirmationText.of(pending)
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(screen.title, style = MaterialTheme.typography.titleMedium)
            screen.lines.forEach { Text(it, style = MaterialTheme.typography.bodyMedium) }
            screen.warning?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = onApprove) { Text(if (screen.needsStepUp) "Verify and approve" else "Approve") }
                OutlinedButton(onClick = onDecline) { Text("Decline") }
            }
        }
    }
}
