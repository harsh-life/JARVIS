package com.hypermind.jarvis

import android.content.Context
import com.hypermind.jarvis.action.GuardedOperations
import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.DeviceKey
import com.hypermind.jarvis.auth.DeviceKeyStore
import com.hypermind.jarvis.auth.EnrollmentStore
import com.hypermind.jarvis.auth.KeystoreDeviceKeyStore
import com.hypermind.jarvis.auth.LoginCoordinator
import com.hypermind.jarvis.auth.Revocation
import com.hypermind.jarvis.auth.SessionManager
import com.hypermind.jarvis.channel.ChannelCredentials
import com.hypermind.jarvis.channel.DeviceChannel
import com.hypermind.jarvis.contract.DeviceGuard
import com.hypermind.jarvis.contract.DeviceLocalState
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.perception.BatteryPrimitive
import com.hypermind.jarvis.perception.JarvisAccessibilityService
import com.hypermind.jarvis.perception.JarvisNotificationListener
import com.hypermind.jarvis.perception.MlKitScreenOcr
import com.hypermind.jarvis.perception.NotificationPrimitive
import com.hypermind.jarvis.perception.Platforms
import com.hypermind.jarvis.perception.ScreenPerception
import com.hypermind.jarvis.permissions.AppPolicyStore
import com.hypermind.jarvis.permissions.GridStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import okhttp3.OkHttpClient
import java.io.IOException
import java.time.Instant
import java.util.concurrent.TimeUnit

/**
 * The app's object graph — deliberately plain constructor wiring, so every
 * security-relevant collaborator is visible in one place. There is no model,
 * router, policy engine or secret here: the server decides; this device
 * authenticates, perceives, executes and reports (docs/23 §0).
 */
class AppGraph(
    context: Context,
    private val mapping: MappingState,
    keyStore: DeviceKeyStore = KeystoreDeviceKeyStore(context),
    primitives: Map<String, Primitive> = defaultPrimitives(context),
) {
    val store = EnrollmentStore(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE))
    val keys: DeviceKeyStore = keyStore
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    val http: OkHttpClient =
        OkHttpClient
            .Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .pingInterval(30, TimeUnit.SECONDS)
            .build()

    private var cachedKey: DeviceKey? = null

    private fun key(): DeviceKey? = cachedKey ?: keys.load()?.also { cachedKey = it }

    fun api(): ApiClient = ApiClient(http, requireNotNull(store.serverUrl) { "no server configured" })

    val sessions = SessionManager(api = ::api, key = ::key, deviceId = { store.deviceId })

    val revocation = Revocation(keys, store, sessions)

    val login = LoginCoordinator(store, keys, ::api, sessions)

    /** The user's per-app grid — local, refusal only (docs/23 §5.2). */
    val grid = GridStore(context.getSharedPreferences(GRID_PREFS, Context.MODE_PRIVATE))

    /** The cached sensitive-app classification. */
    val appPolicy = AppPolicyStore(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE))

    private var guard: Pair<String, DeviceGuard>? = null

    /** The guard for the currently enrolled device (re-created on re-enrollment). */
    @Synchronized
    private fun guard(): DeviceGuard? {
        val device = store.deviceId?.toString() ?: return null
        val valid = mapping as? MappingState.Valid ?: return null
        guard?.takeIf { it.first == device }?.let { return it.second }
        return DeviceGuard(device, valid.mapping).also { guard = device to it }
    }

    private val handler =
        GuardedOperations(
            guard = ::guard,
            localState = { DeviceLocalState(grid.state(), appPolicy.current()) },
            primitives = primitives,
            available = Platforms::available,
        )

    init {
        revocation.onWipe(grid::wipe)
        revocation.onWipe(appPolicy::wipe)
    }

    private fun refreshAppPolicy() {
        try {
            appPolicy.update(api().appPolicy(sessions.accessToken().first))
        } catch (ignored: IOException) {
            // Kept as it was; with nothing cached the guard is restrictive.
        } catch (ignored: com.hypermind.jarvis.auth.EnrollmentLost) {
            // The channel handles a lost enrollment itself.
        }
    }

    val channel: DeviceChannel =
        DeviceChannel(
            http = http,
            url = { store.serverUrl?.let { ApiClient(http, it).channelUrl() } },
            credentials =
                object : ChannelCredentials {
                    override fun accessToken(forceRefresh: Boolean): Pair<String, Instant> =
                        sessions.accessToken(forceRefresh)

                    override fun freshProof(): String = sessions.freshProof()
                },
            mappingVersion = (mapping as? MappingState.Valid)?.mapping?.version ?: "invalid",
            clientVersion = BuildConfig.VERSION_NAME,
            handler = handler,
            scope = scope,
            onRevoked = {
                cachedKey = null
                revocation.wipe()
            },
            onConnected = {
                refreshAppPolicy()
                reportPlatforms()
            },
        )

    private fun reportPlatforms() {
        channel.reportPlatforms(Platforms.snapshot())
    }

    init {
        // The server hears when the Accessibility service comes or goes, so a
        // task waiting on it can be told why (informational, never authority).
        JarvisAccessibilityService.onAvailabilityChanged(::reportPlatforms)
    }

    val enrolled: Boolean get() = store.deviceId != null && key() != null

    fun forgetKey() {
        cachedKey = null
    }

    private companion object {
        const val PREFS = "jarvis"
        const val GRID_PREFS = "jarvis_grid"

        /**
         * The primitives this client implements, keyed by the mapping's
         * primitive name. A mapped primitive missing here fails explicitly
         * (`action_failed`) — it is never approximated by another one.
         */
        fun defaultPrimitives(context: Context): Map<String, Primitive> {
            val screen =
                ScreenPerception(
                    screen = JarvisAccessibilityService.screen,
                    ocr = MlKitScreenOcr { JarvisAccessibilityService.instance },
                )
            return mapOf(
                "accessibility.read_tree" to screen.readTree,
                "accessibility.read_element" to screen.readElement,
                "android.api.battery_state" to BatteryPrimitive(BatteryPrimitive.reader(context.applicationContext)),
                "android.api.notification_query" to NotificationPrimitive(JarvisNotificationListener::active),
            )
        }
    }
}
