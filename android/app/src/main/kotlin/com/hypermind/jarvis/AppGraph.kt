package com.hypermind.jarvis

import android.content.Context
import com.hypermind.jarvis.action.GuardedOperations
import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.DeviceKey
import com.hypermind.jarvis.auth.DeviceKeyStore
import com.hypermind.jarvis.auth.EnrollmentStore
import com.hypermind.jarvis.auth.KeystoreDeviceKeyStore
import com.hypermind.jarvis.auth.KeystoreStepUpKeyStore
import com.hypermind.jarvis.auth.LoginCoordinator
import com.hypermind.jarvis.auth.Revocation
import com.hypermind.jarvis.auth.SessionManager
import com.hypermind.jarvis.auth.StepUpFlow
import com.hypermind.jarvis.auth.StepUpKeyStore
import com.hypermind.jarvis.channel.ChannelCredentials
import com.hypermind.jarvis.channel.ChannelService
import com.hypermind.jarvis.channel.ConnectionIntent
import com.hypermind.jarvis.channel.DeviceChannel
import com.hypermind.jarvis.contract.DeviceGuard
import com.hypermind.jarvis.contract.DeviceLocalState
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.perception.AccessibilityScreenCapture
import com.hypermind.jarvis.perception.BatteryPrimitive
import com.hypermind.jarvis.perception.DeviceActions
import com.hypermind.jarvis.perception.JarvisAccessibilityService
import com.hypermind.jarvis.perception.JarvisNotificationListener
import com.hypermind.jarvis.perception.MlKitScreenOcr
import com.hypermind.jarvis.perception.NotificationPrimitive
import com.hypermind.jarvis.perception.PlatformPrompt
import com.hypermind.jarvis.perception.Platforms
import com.hypermind.jarvis.perception.ScreenPerception
import com.hypermind.jarvis.perception.ScreenshotPrimitive
import com.hypermind.jarvis.perception.UiActions
import com.hypermind.jarvis.permissions.AppPolicyStore
import com.hypermind.jarvis.permissions.GridStore
import com.hypermind.jarvis.permissions.GridSync
import com.hypermind.jarvis.presentation.PerceptionRung
import com.hypermind.jarvis.presentation.PresentationInputs
import com.hypermind.jarvis.presentation.PresentationState
import com.hypermind.jarvis.presentation.PresentationStream
import com.hypermind.jarvis.presentation.Presenter
import com.hypermind.jarvis.presentation.PushStatus
import com.hypermind.jarvis.privileged.ForceStopPrimitive
import com.hypermind.jarvis.privileged.RikkaShizukuGateway
import com.hypermind.jarvis.privileged.ShizukuGateway
import com.hypermind.jarvis.push.FirebasePushClient
import com.hypermind.jarvis.push.PushClient
import com.hypermind.jarvis.push.PushRegistrar
import com.hypermind.jarvis.push.PushSettings
import com.hypermind.jarvis.push.WakeHandler
import com.hypermind.jarvis.reminders.AndroidReminderNotifier
import com.hypermind.jarvis.reminders.ReminderInbox
import com.hypermind.jarvis.reminders.ReminderNotifier
import com.hypermind.jarvis.tasks.TaskController
import com.hypermind.jarvis.tasks.TaskMemory
import com.hypermind.jarvis.tasks.TaskTracker
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
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
    private val shizuku: RikkaShizukuGateway = RikkaShizukuGateway(context.applicationContext),
    primitives: Map<String, Primitive> = defaultPrimitives(context, shizuku),
    reminderNotifier: ReminderNotifier = AndroidReminderNotifier(context.applicationContext),
    pushClient: PushClient = FirebasePushClient(context),
) {
    private val appContext = context.applicationContext

    val platforms = Platforms(shizuku::available)
    private val prompt = PlatformPrompt(context.applicationContext)

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

    /** docs/23 §3: the user-presence-bound key that step-up re-attestation signs with. */
    private val stepUpKeys: StepUpKeyStore = KeystoreStepUpKeyStore()

    val stepUp = StepUpFlow(::api, { sessions.accessToken().first }, stepUpKeys) { store.deviceId }

    /** Tasks from this phone, with the confirmation flow (docs/23 §5.4). */
    val tasks = TaskController(::api, { sessions.accessToken().first }, stepUp)

    val login = LoginCoordinator(store, keys, ::api, sessions, enrollStepUp = stepUp::enroll)

    /** The user's per-app grid — local, refusal only (docs/23 §5.2). */
    val grid = GridStore(context.getSharedPreferences(GRID_PREFS, Context.MODE_PRIVATE))

    /** The cached sensitive-app classification. */
    val appPolicy = AppPolicyStore(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE))

    /** The grid's toggles, backed server-side by the user's own grants (PRD §13). */
    val gridSync =
        GridSync(
            api = ::api,
            accessToken = { sessions.accessToken().first },
            deviceId = { store.deviceId?.toString() },
            mapping = { (mapping as? MappingState.Valid)?.mapping },
            grid = grid,
            policy = appPolicy::current,
        )

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
            available = platforms::available,
            onPlatformMissing = prompt::show,
        )

    init {
        revocation.onWipe(grid::wipe)
        revocation.onWipe(appPolicy::wipe)
        revocation.onWipe(stepUpKeys::destroy)
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

    /** docs/22: reminders shown as notifications — messages, never operations. */
    val reminders = ReminderInbox(deviceId = { store.deviceId?.toString() }, notifier = reminderNotifier)

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
                // A toggle changed while offline is reconciled now; until then
                // the local grid alone refuses.
                gridSync.sync()
                // docs/23 §4: push wake follows the server's offer and the
                // user's choice; a rotated token is re-bound here too.
                push.sync()
                pushState.value = pushStatus()
                reportPlatforms()
                // A task the phone was following (e.g. before a restart) is asked about again.
                taskTracker.reattach()
            },
        )

    init {
        channel.onReminder = reminders::receive
    }

    // ── docs/23 §4 push wake: optional, off by default ──────────────────

    /** Whether the user left JARVIS connected (a wake never overrides Disconnect). */
    val connection = ConnectionIntent(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE))

    private val pushSettings = PushSettings(context.getSharedPreferences(PUSH_PREFS, Context.MODE_PRIVATE))

    val push =
        PushRegistrar(
            api = ::api,
            accessToken = { sessions.accessToken().first },
            enrolled = { enrolled },
            settings = pushSettings,
            client = pushClient,
            scope = scope,
        )

    /** A push is only ever a request to (re)connect the authenticated channel. */
    val wake =
        WakeHandler(
            enrolled = { enrolled },
            connectionWanted = { connection.wanted },
            channel = { channel.wake() },
            startChannelService = {
                try {
                    ChannelService.start(appContext)
                    true
                } catch (ignored: IllegalStateException) {
                    // Background start refused (ForegroundServiceStartNotAllowedException):
                    // the phone reconnects when opened instead.
                    false
                }
            },
        )

    /** A new or rotated token from the push SDK (off the main thread). */
    fun onPushToken(token: String) {
        scope.launch { push.register(token) }
    }

    val pushOffered: Boolean get() = pushSettings.serverProvider == com.hypermind.jarvis.contract.PushProviderKind.FCM

    val pushOptedIn: Boolean get() = pushSettings.optedIn

    init {
        revocation.onWipe(push::wipe)
        revocation.onWipe { connection.wanted = false }
    }

    private fun reportPlatforms() {
        val snapshot = platforms.snapshot()
        platformState.value = snapshot
        channel.reportPlatforms(snapshot)
    }

    init {
        // The server hears when the Accessibility service comes or goes, so a
        // task waiting on it can be told why (informational, never authority).
        JarvisAccessibilityService.onAvailabilityChanged(::reportPlatforms)
        // Shizuku coming back (after a reboot and re-pairing) is reported at
        // once, so a task waiting for it can be resumed server-side.
        shizuku.onAvailabilityChanged(::reportPlatforms)
    }

    /** From the setup screen: ask the user to allow JARVIS in Shizuku. */
    fun requestShizukuPermission() = shizuku.requestPermission()

    val enrolled: Boolean get() = store.deviceId != null && key() != null

    // ── docs/23 §7 presentation: one stream, derived from canonical state ──

    /** The current task, at app scope: the server's last answer, never a local runtime. */
    val taskTracker =
        TaskTracker(tasks, TaskMemory(context.getSharedPreferences(TASK_PREFS, Context.MODE_PRIVATE)), scope)

    private val platformState = MutableStateFlow(platforms.snapshot())
    private val enrolledState = MutableStateFlow(enrolled)
    private val pushState = MutableStateFlow(pushStatus())

    private fun pushStatus(): PushStatus =
        when {
            pushSettings.serverProvider != com.hypermind.jarvis.contract.PushProviderKind.FCM -> PushStatus.NOT_OFFERED
            !pushSettings.optedIn -> PushStatus.OFF
            push.status is PushRegistrar.Status.Unavailable -> PushStatus.UNAVAILABLE
            else -> PushStatus.ON
        }

    /**
     * What every surface shows — the task panel, the overlay, a future
     * character — from one deterministic mapping ([Presenter]).
     */
    val presentation: StateFlow<PresentationState> =
        PresentationStream
            .of(
                channel.state,
                taskTracker.snapshot,
                channel.operations,
                PerceptionRung.level,
                enrolledState,
                platformState,
                pushState,
            ).stateIn(scope, SharingStarted.Eagerly, Presenter.of(PresentationInputs(enrolled, channel.state.value)))

    /** Re-read the inputs that have no callbacks of their own (on returning to the app). */
    fun refreshPresentation() {
        enrolledState.value = enrolled
        platformState.value = platforms.snapshot()
        pushState.value = pushStatus()
    }

    /** The user's push-wake choice, reflected in the presentation at once. */
    fun setPushOptedIn(on: Boolean): PushRegistrar.Status = push.setOptedIn(on).also { pushState.value = pushStatus() }

    init {
        revocation.onWipe(taskTracker::wipe)
        revocation.onWipe { enrolledState.value = false }
    }

    fun forgetKey() {
        cachedKey = null
    }

    private companion object {
        const val PREFS = "jarvis"
        const val GRID_PREFS = "jarvis_grid"
        const val PUSH_PREFS = "jarvis_push"
        const val TASK_PREFS = "jarvis_task"

        /**
         * The primitives this client implements, keyed by the mapping's
         * primitive name. A mapped primitive missing here fails explicitly
         * (`action_failed`) — it is never approximated by another one.
         */
        fun defaultPrimitives(
            context: Context,
            shizuku: ShizukuGateway,
        ): Map<String, Primitive> {
            val screen =
                ScreenPerception(
                    screen = JarvisAccessibilityService.screen,
                    ocr = MlKitScreenOcr { JarvisAccessibilityService.instance },
                    onRung = PerceptionRung::report,
                )
            val ui =
                UiActions(
                    JarvisAccessibilityService.screen,
                    object : DeviceActions {
                        override fun globalAction(name: String) =
                            JarvisAccessibilityService.instance?.globalAction(name) ?: false

                        override fun launch(packageName: String) =
                            JarvisAccessibilityService.instance?.launch(packageName) ?: false
                    },
                )
            return mapOf(
                "accessibility.tap" to ui.tap,
                "accessibility.swipe" to ui.swipe,
                "accessibility.input_text" to ui.inputText,
                "accessibility.global_action" to ui.globalAction,
                "android.intent.launch_activity" to ui.launch,
                "accessibility.read_tree" to screen.readTree,
                "accessibility.read_element" to screen.readElement,
                "android.api.battery_state" to BatteryPrimitive(BatteryPrimitive.reader(context.applicationContext)),
                "android.api.notification_query" to NotificationPrimitive(JarvisNotificationListener::active),
                "shizuku.force_stop_package" to ForceStopPrimitive(shizuku, context.packageName),
                "accessibility.screenshot" to
                    ScreenshotPrimitive(
                        JarvisAccessibilityService.screen,
                        AccessibilityScreenCapture { JarvisAccessibilityService.instance },
                    ),
            )
        }
    }
}
