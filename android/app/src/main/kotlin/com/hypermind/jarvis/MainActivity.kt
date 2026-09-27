package com.hypermind.jarvis

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.BiometricPresence
import com.hypermind.jarvis.channel.ChannelService
import com.hypermind.jarvis.channel.ChannelState
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.contract.VoiceConfigView
import com.hypermind.jarvis.contract.VoicePlacement
import com.hypermind.jarvis.permissions.GridSync
import com.hypermind.jarvis.push.PushRegistrar
import com.hypermind.jarvis.ui.AppGrid
import com.hypermind.jarvis.ui.GridRows
import com.hypermind.jarvis.ui.InstalledApps
import com.hypermind.jarvis.ui.TaskPanel
import com.hypermind.jarvis.ui.TaskPanelState
import com.hypermind.jarvis.ui.VoiceControls
import com.hypermind.jarvis.ui.theme.JarvisTheme
import com.hypermind.jarvis.voice.AndroidOnDeviceRecognizer
import com.hypermind.jarvis.voice.AndroidSpeechEngine
import com.hypermind.jarvis.voice.RecognizerError
import com.hypermind.jarvis.voice.Speaker
import com.hypermind.jarvis.voice.SpeechInput
import com.hypermind.jarvis.voice.VoiceMessages
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException

// A FragmentActivity (still a ComponentActivity for Compose) so the platform
// biometric prompt can attach to it for step-up (docs/23 §3).
class MainActivity : FragmentActivity() {
    private val notice = mutableStateOf<String?>(null)
    private val draft = mutableStateOf<String?>(null)
    private val requestNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    // docs/27: push-to-talk on this phone's own recognizer; system TTS for results.
    private var speech: SpeechInput? = null
    private var speaker: Speaker? = null
    private val requestMicrophone =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) speech?.start() else notice.value = VoiceMessages.of(RecognizerError.PERMISSION)
        }

    private fun listen() {
        val granted =
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        if (granted) speech?.start() else requestMicrophone.launch(Manifest.permission.RECORD_AUDIO)
    }

    override fun onPause() {
        // Interruption: the microphone never stays open behind another app.
        speech?.cancel()
        speaker?.stop()
        super.onPause()
    }

    override fun onDestroy() {
        speech?.release()
        speaker?.shutdown()
        super.onDestroy()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        notice.value = intent?.getStringExtra(EXTRA_NOTICE)
        takeDraft(intent)
        val app = application as JarvisApplication
        speech = SpeechInput(AndroidOnDeviceRecognizer(this), VoiceConfigView.DEFAULT.maxTranscriptChars) {}
        speaker =
            Speaker(
                AndroidSpeechEngine(this),
                enabled = { app.graph.voiceSettings.speakResults },
                maxChars = VoiceConfigView.DEFAULT.maxTtsChars,
            )
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
        takeDraft(intent)
    }

    /** docs/22 §2: "start task" on a reminder pre-fills the task box — nothing is sent. */
    private fun takeDraft(intent: Intent?) {
        val text = intent?.getStringExtra(EXTRA_TASK_DRAFT)?.take(MAX_DRAFT_CHARS) ?: return
        if (text.isBlank()) return
        draft.value = text
        notice.value = getString(R.string.reminder_draft_notice)
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
                    graph.connection.wanted = true
                    ChannelService.start(this)
                }) { Text("Connect") }
            else ->
                OutlinedButton(onClick = {
                    graph.connection.wanted = false
                    ChannelService.stop(this)
                }) { Text("Disconnect") }
        }
        if (state is ChannelState.Connected) ConnectedPanel(app)
        if (graph.pushOffered) PushToggle(app)
        var showGrid by remember { mutableStateOf(false) }
        OutlinedButton(
            onClick = { showGrid = !showGrid },
        ) { Text(if (showGrid) "Hide app permissions" else "App permissions") }
        if (showGrid) Grid(app)
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
                graph.connection.wanted = false
                ChannelService.stop(this@MainActivity)
                graph.forgetKey()
                graph.revocation.wipe()
                enrolled = false
                notice.value = "This phone was removed."
            }
        }) { Text("Remove this phone") }
    }

    /**
     * docs/23 §4: waking this phone through Google Firebase when your server
     * needs it — offered only when the server has it configured, and off until
     * the user turns it on. A wake only reconnects; it never carries or runs
     * anything.
     */
    @Composable
    private fun PushToggle(app: JarvisApplication) {
        val graph = app.graph
        val scope = rememberCoroutineScope()
        var on by remember { mutableStateOf(graph.pushOptedIn) }
        var note by remember { mutableStateOf<String?>(null) }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("Wake this phone when needed (Google Firebase)", style = MaterialTheme.typography.bodyMedium)
            Switch(checked = on, onCheckedChange = { wanted ->
                on = wanted
                scope.launch {
                    val status = withContext(Dispatchers.IO) { graph.push.setOptedIn(wanted) }
                    note =
                        when (status) {
                            is PushRegistrar.Status.Unavailable -> "Push is not available on this phone."
                            is PushRegistrar.Status.Pending -> "Not yet saved on your server; it will retry."
                            else -> null
                        }
                }
            })
        }
        note?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    }

    /** The task box, its confirmation card, and push-to-talk (docs/23 §5.4, docs/27). */
    @Composable
    private fun ConnectedPanel(app: JarvisApplication) {
        val graph = app.graph
        val scope = rememberCoroutineScope()
        var panel by remember { mutableStateOf<TaskPanelState>(TaskPanelState.Idle) }
        var voiceView by remember { mutableStateOf(VoiceConfigView.DEFAULT) }
        LaunchedEffect(Unit) {
            voiceView =
                withContext(Dispatchers.IO) {
                    try {
                        graph.api().voiceConfig(graph.sessions.accessToken().first)
                    } catch (ignored: IOException) {
                        VoiceConfigView.DEFAULT
                    } catch (ignored: com.hypermind.jarvis.auth.EnrollmentLost) {
                        VoiceConfigView.DEFAULT
                    }
                }
        }
        val heard = speech?.state?.collectAsState()
        // Read the answer aloud only if the user turned it on (off by default).
        LaunchedEffect(panel) {
            val answer = panel as? TaskPanelState.Answer
            if (answer != null && voiceView.tts == VoicePlacement.DEVICE) speaker?.speak(answer.text)
        }
        val presence =
            BiometricPresence(
                this,
                title = getString(R.string.step_up_title),
                subtitle = getString(R.string.step_up_subtitle),
                cancel = getString(R.string.step_up_cancel),
            )
        TaskPanel(
            state = panel,
            onSubmit = { text ->
                panel = TaskPanelState.Working
                scope.launch { panel = TaskPanelState.of(graph.tasks.submit(text)) }
            },
            onApprove = { taskId, pending ->
                scope.launch { panel = TaskPanelState.of(graph.tasks.approve(taskId, pending, presence)) }
            },
            onDecline = { taskId, pending ->
                scope.launch { panel = TaskPanelState.of(graph.tasks.decline(taskId, pending)) }
            },
            draft = draft.value,
            voice =
                heard?.value?.takeIf { voiceView.stt == VoicePlacement.DEVICE }?.let { current ->
                    VoiceControls(
                        state = current,
                        onListen = { listen() },
                        onStop = { speech?.stopListening() },
                    )
                },
        )
        if (voiceView.tts == VoicePlacement.DEVICE) SpeakResultsToggle(app)
    }

    @Composable
    private fun SpeakResultsToggle(app: JarvisApplication) {
        var on by remember { mutableStateOf(app.graph.voiceSettings.speakResults) }
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Switch(checked = on, onCheckedChange = {
                on = it
                app.graph.voiceSettings.speakResults = it
                if (!it) speaker?.stop()
            })
            Text("Read results aloud", style = MaterialTheme.typography.bodyMedium)
        }
    }

    /** The per-app grid (PRD §13): local refusal at once, the server's grants reconciled after. */
    @Composable
    private fun Grid(app: JarvisApplication) {
        val graph = app.graph
        val scope = rememberCoroutineScope()
        var version by remember { mutableStateOf(0) }
        var status by remember { mutableStateOf<String?>(null) }
        val apps = remember { InstalledApps.launchable(this) }
        val grid = remember(version) { graph.grid.state() }

        fun after(result: GridSync.Result) {
            status =
                when (result) {
                    GridSync.Result.InSync -> null
                    is GridSync.Result.Pending -> "Not yet saved on your server; turned-off items are already off here."
                }
            version++
        }
        AppGrid(
            rows = GridRows.of(apps, grid, graph.appPolicy.current()),
            deviceState = grid.deviceState,
            status = status,
            onToggle = { pkg, toggle, on ->
                scope.launch { after(withContext(Dispatchers.IO) { graph.gridSync.set(pkg, toggle, on) }) }
            },
            onDeviceState = { on ->
                graph.grid.setDeviceState(on)
                version++
            },
        )
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
        const val EXTRA_TASK_DRAFT = "task_draft"
        private const val MAX_DRAFT_CHARS = 8000
    }
}
