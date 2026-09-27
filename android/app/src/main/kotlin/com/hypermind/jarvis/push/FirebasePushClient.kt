package com.hypermind.jarvis.push

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import com.google.firebase.messaging.FirebaseMessaging
import com.hypermind.jarvis.contract.FcmClientOptions

/**
 * Firebase Cloud Messaging, initialized **only** here and only when the user
 * has turned push wake on and the server offers FCM (docs/23 §4). The
 * manifest removes Firebase's automatic initialization and disables
 * auto-registration, so an app that never reaches [enable] never talks to
 * Firebase at all. The client identifiers come from the user's own server —
 * none are built into the APK.
 */
class FirebasePushClient(
    context: Context,
) : PushClient {
    private val context = context.applicationContext

    override fun enable(
        options: FcmClientOptions,
        onToken: (String) -> Unit,
        onUnavailable: (String) -> Unit,
    ) {
        try {
            app(options)
            setReceiver(enabled = true)
            val messaging = FirebaseMessaging.getInstance()
            messaging.isAutoInitEnabled = true
            messaging.token.addOnCompleteListener { task ->
                val token = if (task.isSuccessful) task.result else null
                if (token.isNullOrEmpty()) onUnavailable("no push token") else onToken(token)
            }
        } catch (e: IllegalStateException) {
            onUnavailable(e.javaClass.simpleName)
        } catch (e: IllegalArgumentException) {
            onUnavailable(e.javaClass.simpleName)
        } catch (e: SecurityException) {
            onUnavailable(e.javaClass.simpleName)
        }
    }

    override fun disable() {
        setReceiver(enabled = false)
        if (FirebaseApp.getApps(context).isEmpty()) return
        try {
            val messaging = FirebaseMessaging.getInstance()
            messaging.isAutoInitEnabled = false
            messaging.deleteToken()
        } catch (ignored: IllegalStateException) {
            // Not initialized: there is no token to delete.
        }
    }

    private fun app(options: FcmClientOptions): FirebaseApp {
        val wanted =
            FirebaseOptions
                .Builder()
                .setProjectId(options.projectId)
                .setApplicationId(options.applicationId)
                .setApiKey(options.apiKey)
                .setGcmSenderId(options.senderId)
                .build()
        val existing = FirebaseApp.getApps(context).firstOrNull { it.name == FirebaseApp.DEFAULT_APP_NAME }
        if (existing != null) {
            if (existing.options == wanted) return existing
            // The server now names another Firebase project: start over.
            existing.delete()
        }
        return FirebaseApp.initializeApp(context, wanted)
    }

    /** Our handler and the library's receiver are on only while push wake is. */
    private fun setReceiver(enabled: Boolean) {
        val state =
            if (enabled) {
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED
            } else {
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED
            }
        for (component in listOf(
            ComponentName(context, JarvisMessagingService::class.java),
            ComponentName(context, FIREBASE_RECEIVER),
        )) {
            context.packageManager.setComponentEnabledSetting(component, state, PackageManager.DONT_KILL_APP)
        }
    }

    private companion object {
        const val FIREBASE_RECEIVER = "com.google.firebase.iid.FirebaseInstanceIdReceiver"
    }
}
