package com.hypermind.jarvis.channel

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import com.hypermind.jarvis.JarvisApplication
import com.hypermind.jarvis.MainActivity
import com.hypermind.jarvis.R
import com.hypermind.jarvis.overlay.AppForeground
import com.hypermind.jarvis.overlay.OverlayActions
import com.hypermind.jarvis.overlay.OverlayController
import com.hypermind.jarvis.overlay.OverlayPolicy
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * Holds the device channel open in a **visible** foreground service while the
 * user has JARVIS connected (docs/23 §4). It does not start itself: the user
 * starts it from the app, and it stops on Disconnect, revocation or an
 * app update being required. Its notification always says what it is doing.
 */
class ChannelService : Service() {
    private val scope = CoroutineScope(SupervisorJob())
    private var watcher: Job? = null
    private var overlayJob: Job? = null
    private var overlay: OverlayController? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        startInForeground("Connecting to your JARVIS server")
        val channel = (application as JarvisApplication).graph.channel
        watcher =
            scope.launch {
                channel.state.collectLatest { state ->
                    when (state) {
                        is ChannelState.Revoked, is ChannelState.UpdateRequired, is ChannelState.Stopped -> stopSelf()
                        else -> notify(describe(state))
                    }
                }
            }
        val callback =
            object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = channel.networkAvailable()
            }
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(callback)
        networkCallback = callback
        channel.start()
        attachOverlay()
    }

    /**
     * docs/23 §7: the floating status overlay lives with this (already
     * visible) foreground service — shown only by [OverlayPolicy], and fed
     * only by the app's one presentation stream.
     */
    private fun attachOverlay() {
        val graph = (application as JarvisApplication).graph
        val controller =
            OverlayController(
                this,
                object : OverlayActions {
                    override fun openApp() {
                        startActivity(
                            Intent(
                                this@ChannelService,
                                MainActivity::class.java,
                            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                        )
                    }

                    override fun cancelTask() {
                        graph.taskTracker.cancel()
                    }

                    override fun hideOverlay() {
                        graph.overlaySettings.set(false)
                    }
                },
            )
        overlay = controller
        overlayJob =
            scope.launch(Dispatchers.Main) {
                combine(
                    graph.presentation,
                    graph.overlaySettings.enabled,
                    AppForeground.visibleCount,
                ) { state, on, fg ->
                    val visible =
                        OverlayPolicy.visible(
                            enabled = on,
                            canDraw = OverlayController.canDraw(this@ChannelService),
                            enrolled = graph.enrolled,
                            appInForeground = fg > 0,
                        )
                    state to visible
                }.collectLatest { (state, visible) -> controller.render(state, visible) }
            }
    }

    override fun onStartCommand(
        intent: Intent?,
        flags: Int,
        startId: Int,
    ): Int = START_NOT_STICKY

    override fun onDestroy() {
        overlayJob?.cancel()
        overlay?.destroy()
        overlay = null
        networkCallback?.let { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) }
        (application as JarvisApplication).graph.channel.stop()
        watcher?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    private fun describe(state: ChannelState): String =
        when (state) {
            is ChannelState.Connected -> "Connected to your JARVIS server"
            is ChannelState.Connecting -> "Connecting to your JARVIS server"
            is ChannelState.Reconnecting -> "Reconnecting (${state.reason})"
            is ChannelState.Disabled -> "The server has the device channel turned off"
            else -> "JARVIS"
        }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Server connection", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Shown whenever JARVIS keeps a connection to your server open."
                },
            )
        }
    }

    private fun build(text: String): Notification {
        val open =
            PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val builder =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Notification.Builder(this, CHANNEL_ID)
            } else {
                @Suppress("DEPRECATION")
                Notification.Builder(this)
            }
        return builder
            .setContentTitle("JARVIS")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    private fun startInForeground(text: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, build(text), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, build(text))
        }
    }

    private fun notify(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, build(text))
    }

    companion object {
        private const val CHANNEL_ID = "jarvis_connection"
        private const val NOTIFICATION_ID = 4210

        fun start(context: Context) {
            val intent = Intent(context, ChannelService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ChannelService::class.java))
        }
    }
}
