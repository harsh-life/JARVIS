package com.hypermind.jarvis.perception

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.contract.BatteryState
import com.hypermind.jarvis.contract.Bounds
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.NotificationItem
import com.hypermind.jarvis.contract.NotificationList
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.resultObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import java.time.Instant

/** `android.api.battery_state` — one sticky-broadcast read, nothing kept. */
class BatteryPrimitive(
    private val read: () -> BatteryState?,
) : Primitive {
    override suspend fun run(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope {
        val state = read() ?: return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        return ResultEnvelope.ok(envelope.opId, resultObject(state))
    }

    companion object {
        fun reader(context: Context): () -> BatteryState? =
            {
                ContextCompat
                    .registerReceiver(
                        context,
                        null,
                        IntentFilter(Intent.ACTION_BATTERY_CHANGED),
                        ContextCompat.RECEIVER_NOT_EXPORTED,
                    )?.let { intent ->
                        batteryState(
                            level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1),
                            scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1),
                            status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1),
                            plugged = intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0),
                        )
                    }
            }

        private const val PERCENT = 100

        fun batteryState(
            level: Int,
            scale: Int,
            status: Int,
            plugged: Int,
        ): BatteryState? {
            if (level < 0 || scale <= 0) return null
            return BatteryState(
                levelPercent = (level * PERCENT / scale).coerceIn(0, PERCENT),
                charging =
                    status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL,
                plugged =
                    when (plugged) {
                        BatteryManager.BATTERY_PLUGGED_AC -> "ac"
                        BatteryManager.BATTERY_PLUGGED_USB -> "usb"
                        BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                        BatteryManager.BATTERY_PLUGGED_DOCK -> "dock"
                        else -> "none"
                    },
            )
        }
    }
}

/** One posted notification, as much of it as this client reads. */
data class PostedNotification(
    val packageName: String,
    val title: String?,
    val text: String?,
    val postedAtMillis: Long,
)

/**
 * `android.api.notification_query` — the currently posted notifications of
 * the **one package the operation names**, newest first, bounded. Queried on
 * demand from the listener; the listener itself keeps nothing.
 */
class NotificationPrimitive(
    private val active: () -> List<PostedNotification>?,
) : Primitive {
    override suspend fun run(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope {
        val posted = active() ?: return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        val limit = envelope.arguments["limit"]?.jsonPrimitive?.int ?: DEFAULT_LIMIT
        val pkg = envelope.packageName ?: return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        return ResultEnvelope.ok(envelope.opId, resultObject(select(posted, pkg, limit)))
    }

    companion object {
        const val DEFAULT_LIMIT = 10

        fun select(
            posted: List<PostedNotification>,
            packageName: String,
            limit: Int,
        ): NotificationList =
            NotificationList(
                posted
                    .asSequence()
                    .filter { it.packageName == packageName }
                    .sortedByDescending { it.postedAtMillis }
                    .take(limit.coerceIn(1, Bounds.MAX_NOTIFICATIONS))
                    .map {
                        NotificationItem(
                            packageName = it.packageName,
                            title = TreeExtractor.clip(it.title, Bounds.MAX_NODE_TEXT),
                            text = TreeExtractor.clip(it.text, Bounds.MAX_NODE_TEXT),
                            postedAt = Instant.ofEpochMilli(it.postedAtMillis).toString(),
                        )
                    }.toList(),
            )
    }
}

/**
 * Notification access, when the user grants it. Holds a reference to itself
 * while bound so [NotificationPrimitive] can query on demand; it does no work
 * when a notification is posted and stores none.
 */
class JarvisNotificationListener : NotificationListenerService() {
    override fun onListenerConnected() {
        super.onListenerConnected()
        instance = this
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
        super.onListenerDisconnected()
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    companion object {
        @Volatile
        var instance: JarvisNotificationListener? = null
            private set

        val connected: Boolean get() = instance != null

        fun active(): List<PostedNotification>? =
            try {
                instance?.activeNotifications?.map(::posted)
            } catch (ignored: SecurityException) {
                null
            }

        private fun posted(sbn: StatusBarNotification): PostedNotification {
            val extras = sbn.notification.extras
            return PostedNotification(
                packageName = sbn.packageName,
                title = extras.getCharSequence(NotificationCompat.EXTRA_TITLE)?.toString(),
                text = extras.getCharSequence(NotificationCompat.EXTRA_TEXT)?.toString(),
                postedAtMillis = sbn.postTime,
            )
        }
    }
}
