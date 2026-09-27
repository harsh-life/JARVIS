package com.hypermind.jarvis.push

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.hypermind.jarvis.JarvisApplication

/**
 * The FCM entry point (docs/23 §4). Disabled in the manifest and enabled only
 * while the user has push wake on ([FirebasePushClient]). It hands the data
 * map to [WakeHandler] — which acts only on the exact wake, and then only by
 * (re)connecting the authenticated channel — and new tokens to
 * [PushRegistrar]. It never reads anything else from a message.
 */
class JarvisMessagingService : FirebaseMessagingService() {
    override fun onMessageReceived(message: RemoteMessage) {
        (application as JarvisApplication).graph.wake.onWake(message.data)
    }

    override fun onNewToken(token: String) {
        (application as JarvisApplication).graph.onPushToken(token)
    }
}
