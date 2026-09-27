package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The per-app grid's grants (PRD §13) against the server's own shapes. */
class GridGrantsTest {
    private val doc = ContractJson.parseToJsonElement(Shared.read("grant_samples.json")).jsonObject
    private val device = doc.getValue("device_id").jsonPrimitive.content
    private val mapping = DeviceMapping.load(Shared.read("device_mapping.json"))
    private val listed = ContractJson.decodeFromJsonElement(GrantList.serializer(), doc.getValue("grant_list")).items
    private val notes = "com.example.notes"
    private val classified = AppPolicy(nonSensitive = setOf(notes))

    private fun grid(vararg toggles: GridToggle) = GridState(packages = mapOf(notes to toggles.toSet()))

    @Test
    fun `the request the phone sends is exactly the server's shape`() {
        val request =
            GrantRequest(capability = "app.interact", scopeId = device, resourceScope = mapOf("package_name" to notes))
        assertEquals(
            doc.getValue("grant_request"),
            ContractJson.parseToJsonElement(ContractJson.encodeToString(GrantRequest.serializer(), request)),
        )
    }

    @Test
    fun `only this device's single-app grants are the grid's`() {
        val managed = listed.map { GridGrants.managedPackage(it, device, mapping) }
        assertEquals(listOf(notes, notes, null, null, null), managed)
        val otherDevice = listed[0].copy(principalId = "6f1c2d3e-4a5b-4c6d-8e7f-00000000d999")
        assertNull(GridGrants.managedPackage(otherDevice, device, mapping))
        val twoKeys =
            listed[0].copy(
                resourceScope =
                    JsonObject(
                        mapOf(
                            "package_name" to JsonPrimitive(notes),
                            "x" to JsonPrimitive("y"),
                        ),
                    ),
            )
        assertNull(GridGrants.managedPackage(twoKeys, device, mapping))
        val notAString = listed[0].copy(resourceScope = JsonObject(mapOf("package_name" to JsonPrimitive(1))))
        assertNull(GridGrants.managedPackage(notAString, device, mapping))
        assertNull(GridGrants.managedPackage(listed[0].copy(revokedAt = "2026-09-27T12:00:30Z"), device, mapping))
    }

    @Test
    fun `screen read needs the reading capabilities, ui interaction the acting ones`() {
        assertEquals(
            setOf("device.read", "app.interact"),
            GridGrants.capabilitiesFor(mapping, notes, setOf(GridToggle.SCREEN_READ), classified),
        )
        assertEquals(
            setOf("app.interact", "device.ui_control"),
            GridGrants.capabilitiesFor(mapping, notes, setOf(GridToggle.UI_INTERACTION), classified),
        )
        assertEquals(emptySet<String>(), GridGrants.capabilitiesFor(mapping, notes, emptySet(), classified))
    }

    @Test
    fun `nothing is asked for that the classification would refuse, nor anything device-wide`() {
        // Unclassified: UI interaction and screenshots are not even requested.
        assertEquals(
            emptySet<String>(),
            GridGrants.capabilitiesFor(mapping, notes, setOf(GridToggle.UI_INTERACTION, GridToggle.SCREENSHOT), null),
        )
        // Sensitive: UI yes (server raises the tier), screenshot no.
        val sensitive = AppPolicy(sensitive = setOf(notes))
        assertEquals(
            emptySet<String>(),
            GridGrants.capabilitiesFor(mapping, notes, setOf(GridToggle.SCREENSHOT), sensitive),
        )
        assertTrue(GridGrants.capabilitiesFor(mapping, notes, setOf(GridToggle.UI_INTERACTION), sensitive).isNotEmpty())
        // device_state never becomes a grant: it would span every app.
        val plan = GridGrants.plan(device, mapping, GridState(deviceState = true), classified, emptyList())
        assertTrue(plan.isEmpty)
    }

    @Test
    fun `turning a toggle on asks only for what is missing`() {
        val plan = GridGrants.plan(device, mapping, grid(GridToggle.SCREEN_READ), classified, listed)
        assertEquals(emptyList<String>(), plan.revoke)
        assertEquals(emptyList<GrantRequest>(), plan.create) // both already granted
        val more =
            GridGrants.plan(
                device,
                mapping,
                grid(GridToggle.SCREEN_READ, GridToggle.UI_INTERACTION),
                classified,
                listed,
            )
        assertEquals(listOf("device.ui_control"), more.create.map { it.capability })
        assertTrue(more.create.all { it.scopeId == device && it.resourceScope == mapOf("package_name" to notes) })
    }

    @Test
    fun `turning everything off revokes the grid's grants and nothing else`() {
        val plan = GridGrants.plan(device, mapping, GridState(), classified, listed)
        assertEquals(listOf(listed[0].grantId, listed[1].grantId), plan.revoke)
        assertTrue(plan.create.isEmpty())
    }

    @Test
    fun `a duplicate grant is revoked, the first kept`() {
        val dup = listed[0].copy(grantId = "6f1c2d3e-4a5b-4c6d-8e7f-00000000c099")
        val plan = GridGrants.plan(device, mapping, grid(GridToggle.SCREEN_READ), classified, listed + dup)
        assertEquals(listOf(dup.grantId), plan.revoke)
        assertFalse(plan.revoke.contains(listed[0].grantId))
    }

    @Test
    fun `an unexpected listing is refused rather than half-read`() {
        val bad = doc.getValue("grant_list").toString().replace("\"scope_type\"", "\"scope_kind\"")
        assertTrue(runCatching { ContractJson.decodeFromString(GrantList.serializer(), bad) }.isFailure)
    }
}
