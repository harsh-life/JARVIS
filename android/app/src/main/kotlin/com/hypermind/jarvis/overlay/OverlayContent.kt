package com.hypermind.jarvis.overlay

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.presentation.PresentationSignal
import com.hypermind.jarvis.presentation.PresentationState
import com.hypermind.jarvis.presentation.PresentationText
import com.hypermind.jarvis.presentation.TaskPhase
import com.hypermind.jarvis.presentation.ui.StatusIndicator

/**
 * What the overlay draws: the indicator; tapped, the one-line state and a
 * short detail, with Open / Cancel / Hide. Built from [PresentationState]
 * alone — there is no content here to show.
 */
@Composable
fun OverlayContent(
    state: PresentationState,
    expanded: Boolean,
    onToggle: () -> Unit,
    actions: OverlayActions,
) {
    val words = PresentationText.of(state)
    val signal = PresentationSignal.of(state)
    Column(
        modifier =
            Modifier
                .background(MaterialTheme.colorScheme.surface.copy(alpha = 0.94f), RoundedCornerShape(12.dp))
                .padding(8.dp)
                .widthIn(max = 280.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Row(
            modifier = Modifier.clickable(onClick = onToggle),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatusIndicator(signal, words.headline, size = 40.dp)
            if (expanded) Text(words.headline, style = MaterialTheme.typography.titleSmall)
        }
        if (expanded) {
            words.detail?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                val approve = state.taskStatus == TaskPhase.WAITING_FOR_CONFIRMATION
                // Approving happens only in the app, on the canonical confirmation card.
                TextButton(onClick = actions::openApp) { Text(if (approve) "Review in app" else "Open") }
                if (state.canCancel) TextButton(onClick = actions::cancelTask) { Text("Cancel task") }
                TextButton(onClick = actions::hideOverlay) { Text("Hide") }
            }
        }
    }
}
