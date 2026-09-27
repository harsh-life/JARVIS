package com.hypermind.jarvis.privileged

import android.content.ComponentName
import android.content.Context
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.IBinder
import android.os.Process
import android.os.RemoteException
import com.hypermind.jarvis.BuildConfig
import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.contract.ActionResult
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.resultObject
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import rikka.shizuku.Shizuku
import kotlin.coroutines.resume

/** What one typed Shizuku call came to. */
enum class PrivilegedOutcome {
    DONE,
    FAILED,

    /** Shizuku is not running, not permitted, or the binding died. */
    UNAVAILABLE,
}

/**
 * Shizuku, **on demand** (docs/23 §5.3; the owner's clarification). Nothing
 * here runs continuously: availability is read at the moment an operation
 * needs it, the privileged service is bound for that one call and unbound
 * after, and a binding lost at any point (a reboot, wireless debugging
 * re-paired, Shizuku stopped) is reported as unavailable — never worked around
 * with another mechanism, and never retried with the old operation.
 */
interface ShizukuGateway {
    fun available(): Boolean

    suspend fun forceStop(packageName: String): PrivilegedOutcome
}

class RikkaShizukuGateway(
    private val context: Context,
) : ShizukuGateway {
    override fun available(): Boolean =
        try {
            Shizuku.pingBinder() &&
                !Shizuku.isPreV11() &&
                Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        } catch (ignored: IllegalStateException) {
            false
        }

    override suspend fun forceStop(packageName: String): PrivilegedOutcome {
        if (!available()) return PrivilegedOutcome.UNAVAILABLE
        val args =
            Shizuku
                .UserServiceArgs(ComponentName(context.packageName, PrivilegedService::class.java.name))
                .daemon(false)
                .processNameSuffix("privileged")
                .debuggable(BuildConfig.DEBUG)
                .version(BuildConfig.VERSION_CODE)
        var connection: ServiceConnection? = null
        return try {
            val service =
                withTimeoutOrNull(BIND_TIMEOUT_MILLIS) {
                    suspendCancellableCoroutine<IJarvisPrivileged?> { cont ->
                        val conn =
                            object : ServiceConnection {
                                override fun onServiceConnected(
                                    name: ComponentName?,
                                    binder: IBinder?,
                                ) {
                                    val alive = binder?.takeIf { it.pingBinder() }
                                    if (cont.isActive) cont.resume(alive?.let(IJarvisPrivileged.Stub::asInterface))
                                }

                                override fun onServiceDisconnected(name: ComponentName?) = Unit
                            }
                        connection = conn
                        Shizuku.bindUserService(args, conn)
                    }
                } ?: return PrivilegedOutcome.UNAVAILABLE
            if (service.forceStopPackage(packageName, Process.myUid() / PER_USER_RANGE)) {
                PrivilegedOutcome.DONE
            } else {
                PrivilegedOutcome.FAILED
            }
        } catch (ignored: RemoteException) {
            // Includes DeadObjectException: the binding died mid-call.
            PrivilegedOutcome.UNAVAILABLE
        } catch (ignored: IllegalStateException) {
            PrivilegedOutcome.UNAVAILABLE
        } finally {
            connection?.let {
                try {
                    Shizuku.unbindUserService(args, it, true)
                } catch (ignored: IllegalStateException) {
                    // Shizuku already gone: nothing left to unbind.
                }
            }
        }
    }

    /** Called whenever Shizuku appears, disappears, or the user answers its permission prompt. */
    fun onAvailabilityChanged(listener: () -> Unit) {
        Shizuku.addBinderReceivedListenerSticky { listener() }
        Shizuku.addBinderDeadListener { listener() }
        Shizuku.addRequestPermissionResultListener { _, _ -> listener() }
    }

    /** Ask the user, in Shizuku's own dialog, to allow JARVIS (from the setup screen only). */
    fun requestPermission() {
        try {
            if (Shizuku.pingBinder() && Shizuku.checkSelfPermission() != PackageManager.PERMISSION_GRANTED) {
                Shizuku.requestPermission(PERMISSION_REQUEST)
            }
        } catch (ignored: IllegalStateException) {
            // Shizuku not running: the prompt tells the user to start it.
        }
    }

    private companion object {
        const val BIND_TIMEOUT_MILLIS = 10_000L
        const val PER_USER_RANGE = 100_000
        const val PERMISSION_REQUEST = 7301
    }
}

/**
 * `shizuku.force_stop_package` — the one Shizuku-backed primitive. Typed: the
 * package is the envelope's own validated `package_name`, and the primitive
 * takes no arguments at all.
 */
class ForceStopPrimitive(
    private val shizuku: ShizukuGateway,
    private val ownPackage: String,
) : Primitive {
    override suspend fun run(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope {
        val target = envelope.packageName ?: return ResultEnvelope.failed(envelope.opId, FailureReason.INTERNAL)
        if (target == ownPackage) return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        return when (shizuku.forceStop(target)) {
            PrivilegedOutcome.DONE -> ResultEnvelope.ok(envelope.opId, resultObject(ActionResult()))
            PrivilegedOutcome.FAILED -> ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
            PrivilegedOutcome.UNAVAILABLE ->
                ResultEnvelope.refused(envelope.opId, RefusalReason.PLATFORM_UNAVAILABLE, PlatformDependency.SHIZUKU)
        }
    }
}
