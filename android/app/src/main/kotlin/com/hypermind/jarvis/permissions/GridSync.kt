package com.hypermind.jarvis.permissions

import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.auth.EnrollmentLost
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.GridGrants
import com.hypermind.jarvis.contract.GridToggle
import java.io.IOException

/**
 * Keeps the server's grants in step with the user's grid (PRD §13): an ON
 * toggle is backed by a device-scoped, single-app `CapabilityGrant`; OFF
 * revokes it.
 *
 * Order matters. Turning a toggle **off** takes effect locally first — the
 * guard refuses the very next operation even if the server cannot be reached
 * — and the revocation follows. Turning one **on** changes nothing until the
 * server has recorded the grant: the local toggle alone never authorizes
 * anything. A sync that fails leaves the local refusal in place and is
 * retried on the next connection.
 */
class GridSync(
    private val api: () -> ApiClient,
    private val accessToken: () -> String,
    private val deviceId: () -> String?,
    private val mapping: () -> DeviceMapping?,
    private val grid: LocalGrid,
    private val policy: () -> AppPolicy?,
) {
    sealed interface Result {
        /** The server's grants match the grid. */
        data object InSync : Result

        /** Not reached, or refused: the local grid still applies; retried later. */
        data class Pending(
            val reason: String,
        ) : Result
    }

    fun set(
        packageName: String,
        toggle: GridToggle,
        on: Boolean,
    ): Result {
        grid.set(packageName, toggle, on)
        return sync()
    }

    /** Reconcile once: revoke first, then create. */
    @Synchronized
    fun sync(): Result {
        val device = deviceId() ?: return Result.Pending("not enrolled")
        val table = mapping() ?: return Result.Pending("no valid mapping")
        return try {
            val token = accessToken()
            val client = api()
            val plan = GridGrants.plan(device, table, grid.state(), policy(), client.listGrants(token).items)
            plan.revoke.forEach { client.revokeGrant(token, it) }
            plan.create.forEach { client.createGrant(token, it) }
            Result.InSync
        } catch (e: IOException) {
            Result.Pending(e.message ?: "unreachable")
        } catch (e: IllegalArgumentException) {
            // A listing that does not parse strictly is not acted on.
            Result.Pending(e.message ?: "malformed response")
        } catch (e: EnrollmentLost) {
            Result.Pending(e.message ?: "enrollment lost")
        }
    }
}
