package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * The device-side guard — the second layer of 08 §4's two-layer enforcement,
 * docs/23 §5.2. Run on every operation before anything executes; the same
 * rules as the server's reference guard (`tests/fake_device.py`), held to the
 * same shared conformance vectors.
 *
 * It can only refuse. Nothing it holds — the per-app grid, the cached
 * sensitive-app classification — can make an operation allowed that the
 * server did not send, choose another package, or substitute an operation.
 */
class DeviceGuard(
    private val deviceId: String,
    private val mapping: DeviceMapping,
    private val clock: () -> Instant = Instant::now,
) {
    private val seen = LinkedHashMap<String, Instant>()

    @Synchronized
    fun check(
        envelope: OperationEnvelope,
        state: DeviceLocalState,
    ): GuardVerdict {
        checkIdentity(envelope, clock())?.let { return GuardVerdict.Refused(it) }
        // 6. a triple in that table
        val spec = mapping.lookup(envelope.capability, envelope.operation)
        if (spec == null || spec.primitive != envelope.primitive) {
            return GuardVerdict.Refused(RefusalReason.NOT_IN_MAPPING)
        }
        return checkScope(envelope, spec, state)
    }

    /** Steps 1–5: this device, first time, a sane unexpired window, same table. */
    private fun checkIdentity(
        envelope: OperationEnvelope,
        now: Instant,
    ): RefusalReason? {
        // 1. addressed to this device
        if (envelope.deviceId != deviceId) return RefusalReason.WRONG_DEVICE
        // 2. never the same operation twice
        forgetExpired(now)
        if (seen.containsKey(envelope.opId)) return RefusalReason.DUPLICATE_OPERATION
        // 3. a well-formed, bounded window
        val issued = parseInstant(envelope.issuedAt)
        val expires = parseInstant(envelope.expiresAt)
        remember(envelope.opId, expires ?: now.plus(MAX_TTL))
        val windowOk =
            issued != null &&
                expires != null &&
                expires.isAfter(issued) &&
                Duration.between(issued, expires) <= MAX_TTL &&
                !issued.isAfter(now.plus(MAX_CLOCK_SKEW))
        return when {
            !windowOk -> RefusalReason.MALFORMED_ARGUMENTS
            // 4. still valid
            !now.isBefore(expires) -> RefusalReason.OPERATION_EXPIRED
            // 5. the same table the server used
            envelope.mappingVersion != mapping.version -> RefusalReason.MAPPING_VERSION_MISMATCH
            else -> null
        }
    }

    private fun checkScope(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
        state: DeviceLocalState,
    ): GuardVerdict {
        // 7. exactly the app scope the primitive needs
        val pkg = envelope.packageName
        val scopeOk =
            when (spec.packageScope) {
                PackageScope.REQUIRED -> pkg != null && Bounds.PACKAGE_NAME.matches(pkg)
                PackageScope.FORBIDDEN -> pkg == null
            }
        if (!scopeOk) return GuardVerdict.Refused(RefusalReason.MALFORMED_ARGUMENTS)
        // 8. the cached classification — only ever narrower than the server
        val policy = state.appPolicy ?: AppPolicy()
        val permittedByPolicy =
            when (spec.gridToggle) {
                GridToggle.UI_INTERACTION -> pkg != null && policy.isClassified(pkg)
                GridToggle.SCREENSHOT -> pkg != null && pkg in policy.nonSensitive
                else -> true
            }
        if (!permittedByPolicy) return GuardVerdict.Refused(RefusalReason.SENSITIVE_PACKAGE)
        // 9. the user's own per-app grid still allows it
        val toggleOn =
            if (spec.gridToggle == GridToggle.DEVICE_STATE) {
                state.grid.deviceState
            } else {
                spec.gridToggle in state.grid.packages[pkg].orEmpty()
            }
        if (!toggleOn) return GuardVerdict.Refused(RefusalReason.TOGGLE_OFF)
        // 10. well-formed arguments
        if (ArgumentValidator.problem(spec, envelope.arguments) != null) {
            return GuardVerdict.Refused(RefusalReason.MALFORMED_ARGUMENTS)
        }
        return GuardVerdict.Allowed(spec)
    }

    private fun remember(
        opId: String,
        until: Instant,
    ) {
        seen[opId] = until
        while (seen.size > MAX_REMEMBERED) seen.remove(seen.keys.first())
    }

    private fun forgetExpired(now: Instant) {
        val horizon = now.minus(MAX_CLOCK_SKEW)
        seen.entries.removeAll { it.value.isBefore(horizon) }
    }

    private fun parseInstant(value: String): Instant? =
        try {
            OffsetDateTime.parse(value).toInstant()
        } catch (ignored: DateTimeParseException) {
            null
        }

    /** Pre-seed already-seen operation ids (for tests of replay refusal). */
    @Synchronized
    fun markSeen(
        opId: String,
        until: Instant,
    ) = remember(opId, until)

    companion object {
        val MAX_TTL: Duration = Duration.ofSeconds(Bounds.MAX_OPERATION_TTL_SECONDS)
        val MAX_CLOCK_SKEW: Duration = Duration.ofSeconds(60)
        private const val MAX_REMEMBERED = 4096
    }
}

sealed interface GuardVerdict {
    data class Allowed(
        val spec: PrimitiveSpec,
    ) : GuardVerdict

    data class Refused(
        val reason: RefusalReason,
    ) : GuardVerdict
}

/** The per-app grid as the guard sees it (PRD §13) — device-local, refusal only. */
data class GridState(
    val packages: Map<String, Set<GridToggle>> = emptyMap(),
    val deviceState: Boolean = false,
)

/** The cached sensitive-app classification (`GET /devices/app-policy`). */
@Serializable
data class AppPolicy(
    @SerialName("non_sensitive") val nonSensitive: Set<String> = emptySet(),
    val sensitive: Set<String> = emptySet(),
    val payment: Set<String> = emptySet(),
) {
    fun isClassified(pkg: String): Boolean = pkg in nonSensitive || pkg in sensitive || pkg in payment
}

data class DeviceLocalState(
    val grid: GridState,
    val appPolicy: AppPolicy?,
)
