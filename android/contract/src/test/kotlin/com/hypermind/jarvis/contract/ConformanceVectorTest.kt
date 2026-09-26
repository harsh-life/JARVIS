package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.time.OffsetDateTime

/**
 * docs/23 §9: the shared conformance vectors, run through this client's
 * [DeviceGuard]. The server runs the same file through its reference guard and
 * its fake transport; a rule that differs between the two fails here or there.
 */
class ConformanceVectorTest {
    private val vectors = ContractJson.parseToJsonElement(Shared.read("conformance_vectors.json")).jsonObject
    private val mapping = DeviceMapping.load(Shared.read("device_mapping.json"))
    private val now: Instant = OffsetDateTime.parse(vectors.getValue("now").jsonPrimitive.content).toInstant()

    private fun state(case: JsonObject): Pair<DeviceLocalState, List<String>> {
        val merged = vectors.getValue("default_state").jsonObject.toMutableMap()
        case["state"]?.jsonObject?.let { merged.putAll(it) }
        val grid = merged.getValue("grid").jsonObject
        val packages =
            grid.getValue("packages").jsonObject.mapValues { (_, toggles) ->
                toggles.jsonArray.map { ContractJson.decodeFromJsonElement(GridToggle.serializer(), it) }.toSet()
            }
        val policyJson = merged.getValue("app_policy")
        val policy =
            if (policyJson is JsonNull) null else ContractJson.decodeFromJsonElement(AppPolicy.serializer(), policyJson)
        val seen = merged.getValue("previously_seen").jsonArray.map { it.jsonPrimitive.content }
        return DeviceLocalState(GridState(packages, grid.getValue("device_state").jsonPrimitive.boolean), policy) to
            seen
    }

    @Test
    fun `the vectors are for this mapping`() {
        assertEquals(mapping.version, vectors.getValue("mapping_version").jsonPrimitive.content)
    }

    @Test
    fun `every vector gets the expected verdict`() {
        val cases = vectors.getValue("cases").jsonArray
        assertTrue(cases.size >= 20)
        val failures = mutableListOf<String>()
        for (element in cases) {
            val case = element.jsonObject
            val name = case.getValue("name").jsonPrimitive.content
            val envelope = ContractJson.decodeFromJsonElement(OperationEnvelope.serializer(), case.getValue("envelope"))
            val (local, seen) = state(case)
            val guard = DeviceGuard(vectors.getValue("device_id").jsonPrimitive.content, mapping) { now }
            seen.forEach { guard.markSeen(it, now.plusSeconds(60)) }
            val verdict = guard.check(envelope, local)
            val expected = case.getValue("expected").jsonObject
            val ok =
                when (expected.getValue("outcome").jsonPrimitive.content) {
                    "allowed" -> verdict is GuardVerdict.Allowed
                    else -> {
                        val reason =
                            ContractJson.decodeFromJsonElement(
                                RefusalReason.serializer(),
                                expected.getValue("reason"),
                            )
                        verdict == GuardVerdict.Refused(reason)
                    }
                }
            if (!ok) failures += "$name: got $verdict, expected $expected"
        }
        assertTrue(failures.joinToString("\n"), failures.isEmpty())
    }

    @Test
    fun `the same operation is refused the second time`() {
        val case =
            vectors
                .getValue("cases")
                .jsonArray
                .first()
                .jsonObject
        val envelope = ContractJson.decodeFromJsonElement(OperationEnvelope.serializer(), case.getValue("envelope"))
        val guard = DeviceGuard(vectors.getValue("device_id").jsonPrimitive.content, mapping) { now }
        val local = state(case).first
        assertTrue(guard.check(envelope, local) is GuardVerdict.Allowed)
        assertEquals(GuardVerdict.Refused(RefusalReason.DUPLICATE_OPERATION), guard.check(envelope, local))
    }
}
