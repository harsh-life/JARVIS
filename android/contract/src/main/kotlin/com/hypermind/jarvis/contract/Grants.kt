package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * The per-app grid's server side (PRD §13): each ON toggle is backed by a
 * `CapabilityGrant` the user consents to with `POST /capabilities`, and a
 * toggle going OFF revokes it. Strict mirrors of the server's `GrantRequest`
 * and `GrantListResponse`, held to `shared/android/grant_samples.json`.
 *
 * The grants are the server's ceiling; the local grid stays the device's own
 * refusal layer (docs/23 §5.2). A grant the phone asks for is still validated
 * server-side (its own device id only, known scope keys, never an
 * absolute-floor capability) — the phone proposes consent, it decides nothing.
 */
@Serializable
data class GrantRequest(
    val capability: String,
    @SerialName("scope_type") val scopeType: String = DEVICE_SCOPE,
    @SerialName("scope_id") val scopeId: String,
    @SerialName("resource_scope") val resourceScope: Map<String, String>,
)

@Serializable
data class CapabilityGrantView(
    @SerialName("grant_id") val grantId: String,
    @SerialName("principal_id") val principalId: String,
    @SerialName("scope_type") val scopeType: String,
    val capability: String,
    // A dict of anything server-side; only a string-valued `package_name`
    // narrowing is the grid's.
    @SerialName("resource_scope") val resourceScope: JsonObject? = null,
    @SerialName("granted_by") val grantedBy: String,
    @SerialName("created_at") val createdAt: String,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("revoked_at") val revokedAt: String? = null,
)

@Serializable
data class GrantList(
    val items: List<CapabilityGrantView>,
)

/** What to change server-side so the grants match the grid. */
data class GridSyncPlan(
    val create: List<GrantRequest>,
    val revoke: List<String>,
) {
    val isEmpty: Boolean get() = create.isEmpty() && revoke.isEmpty()
}

const val DEVICE_SCOPE = "device"

object GridGrants {
    /**
     * The capabilities an app's ON toggles need, from the shared mapping —
     * only for toggles the cached classification lets through (the same rule
     * as the guard's step 8), and never `device_state`: `read_battery` takes
     * no app, so its grant would be device-wide and cover every app's screen
     * too. The phone never asks for a grant wider than one app.
     */
    fun capabilitiesFor(
        mapping: DeviceMapping,
        packageName: String,
        toggles: Set<GridToggle>,
        policy: AppPolicy?,
    ): Set<String> {
        val allowed = toggles.filter { permitted(it, packageName, policy ?: AppPolicy()) }.toSet()
        return mapping.capabilityNames
            .filter { capability ->
                mapping.operations(capability).any { operation ->
                    val spec = mapping.lookup(capability, operation)
                    spec != null && spec.packageScope == PackageScope.REQUIRED && spec.gridToggle in allowed
                }
            }.toSet()
    }

    /**
     * The grid's own grants are exactly: scoped to this device, narrowed to
     * precisely one app, for a capability in the shared mapping, not revoked.
     * Anything else (a user-wide grant from the dashboard, an unscoped one)
     * is not the grid's to revoke.
     */
    fun managedPackage(
        grant: CapabilityGrantView,
        deviceId: String,
        mapping: DeviceMapping,
    ): String? {
        if (grant.scopeType != DEVICE_SCOPE || grant.principalId != deviceId || grant.revokedAt != null) return null
        if (grant.capability !in mapping.capabilityNames) return null
        val scope = grant.resourceScope ?: return null
        val pkg = (scope[PACKAGE_KEY] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        return pkg.takeIf { scope.size == 1 && Bounds.PACKAGE_NAME.matches(it) }
    }

    fun plan(
        deviceId: String,
        mapping: DeviceMapping,
        grid: GridState,
        policy: AppPolicy?,
        grants: List<CapabilityGrantView>,
    ): GridSyncPlan {
        val managed = grants.mapNotNull { g -> managedPackage(g, deviceId, mapping)?.let { it to g } }
        val packages = (grid.packages.keys + managed.map { it.first }).filter { Bounds.PACKAGE_NAME.matches(it) }
        val create = mutableListOf<GrantRequest>()
        val revoke = mutableListOf<String>()
        for (pkg in packages.sorted()) {
            val wanted = capabilitiesFor(mapping, pkg, grid.packages[pkg].orEmpty(), policy)
            val kept = mutableSetOf<String>()
            for ((_, grant) in managed.filter { it.first == pkg }) {
                // Unwanted, or a duplicate of one already kept: revoke.
                if (grant.capability in wanted && kept.add(grant.capability)) continue
                revoke += grant.grantId
            }
            (wanted - kept).sorted().forEach {
                create += GrantRequest(capability = it, scopeId = deviceId, resourceScope = mapOf(PACKAGE_KEY to pkg))
            }
        }
        return GridSyncPlan(create, revoke)
    }

    private fun permitted(
        toggle: GridToggle,
        pkg: String,
        policy: AppPolicy,
    ): Boolean =
        when (toggle) {
            GridToggle.UI_INTERACTION -> policy.isClassified(pkg)
            GridToggle.SCREENSHOT -> pkg in policy.nonSensitive
            GridToggle.SCREEN_READ -> true
            GridToggle.DEVICE_STATE -> false
        }

    private const val PACKAGE_KEY = "package_name"
}
