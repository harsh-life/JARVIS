package com.hypermind.jarvis.ui

import android.content.Context
import android.content.Intent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.GridState
import com.hypermind.jarvis.contract.GridToggle

/** One toggle in one app's row. `enabled = false` means the user cannot turn it on here. */
data class GridCell(
    val toggle: GridToggle,
    val label: String,
    val on: Boolean,
    val enabled: Boolean,
    val note: String?,
)

data class GridRow(
    val packageName: String,
    val label: String,
    val cells: List<GridCell>,
)

/**
 * The per-app grid as the user sees it (PRD §13). What a toggle can be set to
 * follows the server's own classification (docs/CAPABILITY_MATRIX.md §5.1):
 * UI control is offered only in an app the owner has classified — "not
 * classified" is never permission — and screenshots only of a non-sensitive
 * one. A toggle shown OFF-and-disabled is refused by the guard whatever is
 * stored, and the sync revokes its grant.
 */
object GridRows {
    fun of(
        apps: List<Pair<String, String>>,
        grid: GridState,
        policy: AppPolicy?,
    ): List<GridRow> {
        val known = policy ?: AppPolicy()
        val labels = apps.toMap()
        val packages = (apps.map { it.first } + grid.packages.keys).distinct()
        return packages
            .map { pkg ->
                val on = grid.packages[pkg].orEmpty()
                GridRow(pkg, labels[pkg] ?: pkg, cells(pkg, on, known))
            }.sortedWith(compareBy({ it.cells.none { c -> c.on } }, { it.label.lowercase() }))
    }

    private fun cells(
        pkg: String,
        on: Set<GridToggle>,
        policy: AppPolicy,
    ): List<GridCell> {
        val classified = policy.isClassified(pkg)
        val nonSensitive = pkg in policy.nonSensitive
        return listOf(
            GridCell(GridToggle.SCREEN_READ, "Read the screen", GridToggle.SCREEN_READ in on, true, null),
            GridCell(
                GridToggle.UI_INTERACTION,
                "Tap and type",
                classified && GridToggle.UI_INTERACTION in on,
                classified,
                when {
                    !classified -> "Not available: your server has not classified this app."
                    pkg in policy.payment -> "A payment app: every action needs your approval and verification."
                    pkg in policy.sensitive -> "A sensitive app: every action needs your approval."
                    else -> null
                },
            ),
            GridCell(
                GridToggle.SCREENSHOT,
                "Screenshots",
                nonSensitive && GridToggle.SCREENSHOT in on,
                nonSensitive,
                if (nonSensitive) null else "Only for apps your server classifies as non-sensitive.",
            ),
        )
    }
}

/** Launchable apps on this phone, as (package, label) — for listing only. */
object InstalledApps {
    fun launchable(context: Context): List<Pair<String, String>> {
        val pm = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return pm
            .queryIntentActivities(intent, 0)
            .map { it.activityInfo.packageName to it.loadLabel(pm).toString() }
            .filter { it.first != context.packageName }
            .distinctBy { it.first }
    }
}

@Composable
fun AppGrid(
    rows: List<GridRow>,
    deviceState: Boolean,
    status: String?,
    onToggle: (String, GridToggle, Boolean) -> Unit,
    onDeviceState: (Boolean) -> Unit,
) {
    Text("What JARVIS may do in each app", style = MaterialTheme.typography.titleMedium)
    Text(
        "Turning something off stops it on this phone at once. Turning it on also needs your server " +
            "to record your consent.",
        style = MaterialTheme.typography.bodySmall,
    )
    status?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error) }
    ToggleLine("Battery level (this phone only)", deviceState, true, onDeviceState)
    rows.forEach { row ->
        Card(modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(row.label, style = MaterialTheme.typography.titleSmall)
                Text(row.packageName, style = MaterialTheme.typography.bodySmall)
                row.cells.forEach { cell ->
                    ToggleLine(cell.label, cell.on, cell.enabled) { onToggle(row.packageName, cell.toggle, it) }
                    cell.note?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                }
            }
        }
    }
}

@Composable
private fun ToggleLine(
    label: String,
    on: Boolean,
    enabled: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium)
        Switch(checked = on, enabled = enabled, onCheckedChange = onChange)
    }
}
