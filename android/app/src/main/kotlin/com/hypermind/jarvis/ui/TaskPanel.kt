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
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.contract.ConfirmationText
import com.hypermind.jarvis.contract.PendingAction
import com.hypermind.jarvis.contract.RiskCategory
import com.hypermind.jarvis.contract.TaskStatus
import com.hypermind.jarvis.contract.TaskView
import com.hypermind.jarvis.presentation.ConnectionPhase
import com.hypermind.jarvis.presentation.ErrorKind
import com.hypermind.jarvis.presentation.PresentationSignal
import com.hypermind.jarvis.presentation.PresentationState
import com.hypermind.jarvis.presentation.PresentationText
import com.hypermind.jarvis.presentation.TaskPhase
import com.hypermind.jarvis.presentation.TaskSnapshot
import com.hypermind.jarvis.presentation.ui.StatusIndicator
import com.hypermind.jarvis.voice.VoiceInputState
import com.hypermind.jarvis.voice.VoiceMessages
import com.hypermind.jarvis.voice.VoiceRouting

/**
 * What the task panel shows below the status header — chosen from the
 * tracker's snapshot (the server's answer) and the one [PresentationState],
 * never re-interpreted. Content lives only here, in the app: the answer, the
 * server's own failure message, and the canonical confirmation card.
 */
sealed interface PanelContent {
    data object Nothing : PanelContent

    /** The server's answer to a completed task. */
    data class Answer(
        val text: String,
    ) : PanelContent

    /** A pending action: rendered only by [ConfirmationCard], from the server's canonical action. */
    data class Confirm(
        val taskId: String,
        val pending: PendingAction,
    ) : PanelContent

    /** The server's own failure message (never model prose), shown under the error wording. */
    data class ServerMessage(
        val text: String,
    ) : PanelContent

    companion object {
        fun of(
            snapshot: TaskSnapshot,
            state: PresentationState,
        ): PanelContent {
            // A revoked phone shows nothing it could act on — not even a pending action.
            if (state.error?.kind == ErrorKind.REVOKED && state.deviceContext.connection == ConnectionPhase.REVOKED) {
                return Nothing
            }
            return when (snapshot) {
                is TaskSnapshot.StepUpBlocked -> Confirm(snapshot.taskId, snapshot.pending)
                is TaskSnapshot.Known -> known(snapshot.view)
                else -> Nothing
            }
        }

        private fun known(view: TaskView): PanelContent =
            when (view) {
                is TaskView.NeedsConfirmation -> Confirm(view.taskId, view.pending)
                is TaskView.Failed -> view.message.takeIf { it.isNotBlank() }?.let(::ServerMessage) ?: Nothing
                is TaskView.Result -> {
                    val result = view.result
                    when (result.status) {
                        TaskStatus.COMPLETED -> result.response?.let(::Answer) ?: Nothing
                        TaskStatus.AWAITING_CONFIRMATION ->
                            result.pending?.let { Confirm(result.taskId, it) }
                                ?: Nothing
                        TaskStatus.FAILED ->
                            result.failure
                                ?.message
                                ?.takeIf { it.isNotBlank() }
                                ?.let(::ServerMessage)
                                ?: Nothing
                        else -> Nothing
                    }
                }
            }
    }
}

/** The headline: indicator + wording + (when there is one) what the user can do. */
@Composable
fun StatusHeader(
    state: PresentationState,
    onAction: (PresentationText.UserAction) -> Unit,
) {
    val words = PresentationText.of(state)
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        StatusIndicator(PresentationSignal.of(state), words.headline)
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(words.headline, style = MaterialTheme.typography.titleMedium)
            words.detail?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        }
    }
    val action = words.action
    if (action != null && action != PresentationText.UserAction.OPEN_APP_TO_APPROVE) {
        OutlinedButton(onClick = { onAction(action) }) { Text(actionLabel(action)) }
    }
}

fun actionLabel(action: PresentationText.UserAction): String =
    when (action) {
        PresentationText.UserAction.OPEN_ACCESSIBILITY_SETTINGS -> "Open Accessibility settings"
        PresentationText.UserAction.OPEN_NOTIFICATION_ACCESS_SETTINGS -> "Open notification access"
        PresentationText.UserAction.OPEN_SHIZUKU -> "Open Shizuku"
        PresentationText.UserAction.CONNECT -> "Connect"
        PresentationText.UserAction.SIGN_IN_AGAIN -> "Sign in again"
        PresentationText.UserAction.UPDATE_APP -> "How to update"
        PresentationText.UserAction.OPEN_APP_TO_APPROVE -> "Review"
        PresentationText.UserAction.TRY_AGAIN -> "Try again"
    }

/** Callbacks the panel raises. Each goes to the task tracker — i.e. to the server. */
class TaskPanelActions(
    val submit: (String) -> Unit,
    val approve: (String, PendingAction) -> Unit,
    val decline: (String, PendingAction) -> Unit,
    val cancel: () -> Unit,
    val retry: () -> Unit,
    val dismiss: () -> Unit,
)

/**
 * The task surface (docs/23 §7): type a task, follow it, answer a pending
 * action, cancel, retry. It holds no task state of its own — the tracker has
 * it (at app scope), so rotation or recreation loses nothing and nothing is
 * sent twice.
 */
@Composable
fun TaskPanel(
    state: PresentationState,
    snapshot: TaskSnapshot,
    busy: Boolean,
    canSubmit: Boolean,
    actions: TaskPanelActions,
    draft: String? = null,
    voice: VoiceControls? = null,
) {
    // A draft (a reminder's words, or a transcript) only fills the box; the
    // user still presses Send, and it goes out as an ordinary task.
    var input by rememberSaveable(draft) { mutableStateOf(draft.orEmpty()) }
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
        enabled = canSubmit && !busy,
    )
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(enabled = canSubmit && !busy && input.isNotBlank(), onClick = { actions.submit(input) }) { Text("Send") }
        if (state.canCancel) OutlinedButton(enabled = !busy, onClick = actions.cancel) { Text("Cancel task") }
        if (state.canRetry) OutlinedButton(enabled = !busy, onClick = actions.retry) { Text("Try again") }
        if (dismissible(state)) TextButton(enabled = !busy, onClick = actions.dismiss) { Text("Clear") }
        voice?.let { VoiceButton(it) }
    }
    voice?.let { VoiceStatus(it.state) }
    when (val content = PanelContent.of(snapshot, state)) {
        PanelContent.Nothing -> Unit
        is PanelContent.Answer -> Text(content.text, style = MaterialTheme.typography.bodyLarge)
        is PanelContent.ServerMessage -> Text(content.text, style = MaterialTheme.typography.bodyMedium)
        is PanelContent.Confirm ->
            ConfirmationCard(
                pending = content.pending,
                busy = busy,
                onApprove = { actions.approve(content.taskId, content.pending) },
                onDecline = { actions.decline(content.taskId, content.pending) },
            )
    }
}

private fun dismissible(state: PresentationState): Boolean =
    state.taskStatus in setOf(TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED) || state.canRetry

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
 * Approving posts the user's answer with the server-issued token; a
 * `high_irreversible` action is verified with step-up first (docs/23 §3).
 */
@Composable
fun ConfirmationCard(
    pending: PendingAction,
    onApprove: () -> Unit,
    onDecline: () -> Unit,
    busy: Boolean = false,
) {
    val screen = ConfirmationText.of(pending)
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(screen.title, style = MaterialTheme.typography.titleMedium)
            Text(riskLabel(pending.riskCategory), style = MaterialTheme.typography.labelMedium)
            screen.lines.forEach { Text(it, style = MaterialTheme.typography.bodyMedium) }
            screen.warning?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (screen.needsStepUp) {
                Text(
                    "Step-up required: you will confirm it is you (fingerprint, face or screen lock) " +
                        "before this is sent.",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Text("This approval expires at ${pending.expiresAt}.", style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(enabled = !busy, onClick = onApprove) {
                    Text(if (screen.needsStepUp) "Verify and approve" else "Approve")
                }
                OutlinedButton(enabled = !busy, onClick = onDecline) { Text("Decline") }
            }
        }
    }
}

fun riskLabel(risk: RiskCategory): String =
    when (risk) {
        RiskCategory.LOW_READ -> "Risk: reads only"
        RiskCategory.LOW_WRITE -> "Risk: low"
        RiskCategory.CONSEQUENTIAL -> "Risk: acts on your behalf"
        RiskCategory.HIGH_IRREVERSIBLE -> "Risk: cannot be undone"
    }
