package com.hypermind.jarvis.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.contract.AgentList
import com.hypermind.jarvis.contract.AgentStatus
import com.hypermind.jarvis.reminders.RunOffer

/**
 * One of the owner's agents as listed (docs/29 §23.3). Display only: "Run"
 * sends the owner's own request; the server decides whether and how it runs.
 */
data class AgentRow(
    val agentId: String,
    val name: String,
    val detail: String,
    val runEnabled: Boolean,
) {
    override fun toString(): String = "AgentRow(agentId=$agentId, runEnabled=$runEnabled)"

    companion object {
        /** Rows for the owner's agents; Run only for an active agent, and only when no task is live. */
        fun of(
            list: AgentList?,
            canRun: Boolean,
        ): List<AgentRow> =
            (list as? AgentList.Shown)?.agents.orEmpty().map { agent ->
                val state =
                    when (agent.status) {
                        AgentStatus.ACTIVE -> agent.triggerDisplay
                        AgentStatus.PAUSED -> "paused"
                        AgentStatus.NEEDS_REAPPROVAL -> "needs your re-approval"
                        else -> agent.status.name.lowercase()
                    }
                AgentRow(agent.agentId, agent.name, state, runEnabled = canRun && agent.runnable)
            }
    }
}

/** The owner's agents (docs/29 Phase 4): each runnable one has Run; results show on the Tasks tab. */
@Composable
fun AgentsPanel(
    list: AgentList?,
    canRun: Boolean,
    onRun: (agentId: String) -> Unit,
    onRefresh: () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        when (list) {
            null -> Text("Loading your agents…", style = MaterialTheme.typography.bodyMedium)
            is AgentList.Failed ->
                Text("Your agents could not be loaded (${list.code}).", style = MaterialTheme.typography.bodyMedium)
            is AgentList.Shown ->
                if (list.agents.isEmpty()) {
                    Text("You have no agents yet.", style = MaterialTheme.typography.bodyMedium)
                }
        }
        AgentRow.of(list, canRun).forEach { row ->
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(row.name, style = MaterialTheme.typography.bodyLarge)
                    Text(row.detail, style = MaterialTheme.typography.bodySmall)
                }
                Button(onClick = { onRun(row.agentId) }, enabled = row.runEnabled) { Text("Run") }
            }
        }
        OutlinedButton(onClick = onRefresh) { Text("Refresh") }
    }
}

/**
 * docs/29 §17.1: "Run agent" from a reminder, in the app — the reminder's own
 * words and a Run the user presses. Nothing has run until then.
 */
@Composable
fun RunOfferCard(
    offer: RunOffer,
    canRun: Boolean,
    onRun: () -> Unit,
    onDismiss: () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(offer.text, style = MaterialTheme.typography.bodyLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onRun, enabled = canRun) { Text("Run") }
            OutlinedButton(onClick = onDismiss) { Text("Not now") }
        }
    }
}
