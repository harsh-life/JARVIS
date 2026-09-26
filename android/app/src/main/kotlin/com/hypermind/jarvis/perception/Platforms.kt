package com.hypermind.jarvis.perception

import android.os.Build
import com.hypermind.jarvis.contract.PlatformDependency

/**
 * Which on-device dependencies are available **right now** (docs/23 §5.3).
 * Read at the moment an operation runs — never cached — so a dependency that
 * went away (the Accessibility service turned off, notification access
 * revoked, Shizuku gone after a reboot) is noticed by the next operation that
 * needs it, which is refused as `platform_unavailable` naming it. Availability
 * never makes anything allowed; it can only refuse.
 */
object Platforms {
    fun available(dependency: PlatformDependency): Boolean =
        when (dependency) {
            PlatformDependency.ACCESSIBILITY_SERVICE -> JarvisAccessibilityService.instance != null
            PlatformDependency.NOTIFICATION_ACCESS -> JarvisNotificationListener.connected
            PlatformDependency.SCREEN_CAPTURE ->
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && JarvisAccessibilityService.instance != null
            // The bundled model ships in the APK.
            PlatformDependency.OCR -> true
            // No typed Shizuku primitive is built into this client yet
            // (Phase F); until then Shizuku is reported unavailable, so the
            // one operation that needs it is refused explicitly, never run
            // some other way.
            PlatformDependency.SHIZUKU -> false
        }

    fun snapshot(): Map<PlatformDependency, Boolean> = PlatformDependency.entries.associateWith(::available)
}
