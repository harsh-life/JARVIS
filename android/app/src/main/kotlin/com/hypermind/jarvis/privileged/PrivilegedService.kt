package com.hypermind.jarvis.privileged

import android.annotation.SuppressLint
import android.os.IBinder
import kotlin.system.exitProcess

/**
 * The Shizuku user service: runs in a separate process that Shizuku starts
 * **on demand** for one operation and destroys afterwards. It exposes exactly
 * the typed calls of [IJarvisPrivileged] — there is no shell, no argv and no
 * generic "run" here (docs/23 §5.3, 08 §6: `shizuku.elevated_shell` stays
 * unexposed).
 *
 * It re-validates its input even though the device guard already did: it is
 * the most privileged code in the app and trusts no caller.
 */
class PrivilegedService : IJarvisPrivileged.Stub() {
    override fun destroy() {
        exitProcess(0)
    }

    override fun forceStopPackage(
        packageName: String,
        userId: Int,
    ): Boolean {
        if (!PACKAGE.matches(packageName) || packageName in PROTECTED || userId < 0) return false
        return try {
            activityManager()
                .javaClass
                .getMethod("forceStopPackage", String::class.java, Int::class.javaPrimitiveType)
                .invoke(activityManager(), packageName, userId)
            true
        } catch (ignored: ReflectiveOperationException) {
            false
        } catch (ignored: SecurityException) {
            false
        }
    }

    // `IActivityManager.forceStopPackage` is not public API; it is reachable
    // here only because this process runs with Shizuku's shell identity.
    @SuppressLint("PrivateApi", "DiscouragedPrivateApi")
    private fun activityManager(): Any {
        val binder =
            Class
                .forName("android.os.ServiceManager")
                .getMethod("getService", String::class.java)
                .invoke(null, "activity") as IBinder
        return Class
            .forName("android.app.IActivityManager\$Stub")
            .getMethod("asInterface", IBinder::class.java)
            .invoke(null, binder)!!
    }

    private companion object {
        val PACKAGE = Regex("^[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+$")

        // Never these, whatever arrives: the system itself, the UI shell,
        // Shizuku, and this app (stopping it would cut its own channel).
        val PROTECTED = setOf("android", "com.android.systemui", "moe.shizuku.privileged.api", "com.hypermind.jarvis")
    }
}
