package com.hypermind.jarvis

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.channel.ChannelService
import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.ui.theme.JarvisTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException

class MainActivity : ComponentActivity() {
    private val notice = mutableStateOf<String?>(null)
    private val requestNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        notice.value = intent?.getStringExtra(EXTRA_NOTICE)
        val app = application as JarvisApplication
        setContent {
            JarvisTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { padding ->
                    Column(
                        modifier = Modifier.padding(padding).padding(20.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        Text("JARVIS", style = MaterialTheme.typography.headlineSmall)
                        notice.value?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
                        when (val mapping = app.mapping) {
                            is MappingState.Invalid ->
                                Text(
                                    "Device actions disabled: ${mapping.reason}",
                                    color = MaterialTheme.colorScheme.error,
                                )
                            is MappingState.Valid -> Setup(app)
                        }
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        notice.value = intent.getStringExtra(EXTRA_NOTICE)
    }

    @Composable
    private fun Setup(app: JarvisApplication) {
        val graph = app.graph
        val scope = rememberCoroutineScope()
        val state by graph.channel.state.collectAsState()
        var enrolled by remember { mutableStateOf(graph.enrolled) }
        var server by remember { mutableStateOf(graph.store.serverUrl.orEmpty()) }

        if (!enrolled || state is ChannelState.Revoked) {
            OutlinedTextField(
                value = server,
                onValueChange = { server = it },
                label = { Text("Your JARVIS server (https://…)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Button(onClick = {
                val valid = ApiClient.validServerUrl(server)
                if (valid == null) {
                    notice.value = "Enter the https:// address of your server."
                    return@Button
                }
                graph.store.serverUrl = valid
                scope.launch {
                    val url =
                        withContext(Dispatchers.IO) {
                            try {
                                graph.login.begin()
                            } catch (ignored: IOException) {
                                null
                            }
                        }
                    if (url == null) {
                        notice.value = "Could not reach the server."
                    } else {
                        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    }
                }
            }) { Text("Sign in with Google") }
            return
        }

        Text("Server: ${graph.store.serverUrl}", style = MaterialTheme.typography.bodySmall)
        Text(describe(state), style = MaterialTheme.typography.bodyMedium)
        when (state) {
            is ChannelState.Stopped, is ChannelState.UpdateRequired, is ChannelState.Revoked ->
                Button(onClick = {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                    ChannelService.start(this)
                }) { Text("Connect") }
            else -> OutlinedButton(onClick = { ChannelService.stop(this) }) { Text("Disconnect") }
        }
        OutlinedButton(onClick = {
            scope.launch {
                withContext(Dispatchers.IO) {
                    val device = graph.store.deviceId
                    try {
                        if (device != null) graph.api().revoke(device, graph.sessions.accessToken().first)
                    } catch (ignored: IOException) {
                        // Removed locally regardless; the server-side revoke can be done from another device.
                    } catch (ignored: com.hypermind.jarvis.auth.EnrollmentLost) {
                        // Already unusable server-side.
                    }
                }
                ChannelService.stop(this@MainActivity)
                graph.forgetKey()
                graph.revocation.wipe()
                enrolled = false
                notice.value = "This phone was removed."
            }
        }) { Text("Remove this phone") }
    }

    private fun describe(state: ChannelState): String =
        when (state) {
            is ChannelState.Stopped -> "Not connected."
            is ChannelState.Connecting -> "Connecting…"
            is ChannelState.Connected -> "Connected."
            is ChannelState.Reconnecting -> "Reconnecting: ${state.reason}."
            is ChannelState.Revoked -> "This phone was removed from your account. Sign in again to re-enroll."
            is ChannelState.UpdateRequired -> "Update the app: it no longer matches your server."
            is ChannelState.Disabled -> "Your server has the device channel turned off."
        }

    companion object {
        const val EXTRA_NOTICE = "notice"
    }
}
