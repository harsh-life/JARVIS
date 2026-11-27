package com.hypermind.jarvis.ui

import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue

/**
 * "Remove this phone" signs the phone out, revokes it on the server and
 * deletes its key; only a new Google sign-in brings it back. So the button
 * only asks: [onConfirmed] runs when the user picks Remove in the dialog, and
 * Cancel, a tap outside or Back leave everything as it was.
 */
@Composable
fun RemovePhoneButton(onConfirmed: () -> Unit) {
    var asking by rememberSaveable { mutableStateOf(false) }
    OutlinedButton(onClick = { asking = true }) { Text(REMOVE_BUTTON) }
    if (asking) {
        AlertDialog(
            onDismissRequest = { asking = false },
            title = { Text(REMOVE_TITLE) },
            text = { Text(REMOVE_TEXT) },
            confirmButton = {
                TextButton(onClick = {
                    asking = false
                    onConfirmed()
                }) { Text(REMOVE_CONFIRM, color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { asking = false }) { Text(REMOVE_CANCEL) } },
        )
    }
}

internal const val REMOVE_BUTTON = "Remove this phone"
internal const val REMOVE_TITLE = "Remove this phone?"
internal const val REMOVE_TEXT =
    "This phone will be signed out of your JARVIS server and its key deleted. " +
        "To use JARVIS here again, you will need to sign in with Google. " +
        "Your account and data on the server are not deleted."
internal const val REMOVE_CONFIRM = "Remove"
internal const val REMOVE_CANCEL = "Cancel"
