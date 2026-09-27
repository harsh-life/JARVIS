package com.hypermind.jarvis.permissions

import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.DeviceMapping
import com.hypermind.jarvis.contract.GridState
import com.hypermind.jarvis.contract.GridToggle
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** The grid ↔ grants sync (PRD §13) against a mock server replaying the server's own shapes. */
class GridSyncTest {
    private val shared = File(requireNotNull(System.getProperty("jarvis.shared.dir")))
    private val samples = ContractJson.parseToJsonElement(shared.resolve("grant_samples.json").readText()).jsonObject
    private val device = samples.getValue("device_id").jsonPrimitive.content
    private val listing = samples.getValue("grant_list").toString()
    private val mapping = DeviceMapping.load(shared.resolve("device_mapping.json").readText())
    private val server = MockWebServer().apply { start() }
    private val notes = "com.example.notes"

    @After
    fun tearDown() = server.shutdown()

    private class MemoryGrid : LocalGrid {
        var packages = mutableMapOf<String, Set<GridToggle>>()

        override fun state() = GridState(packages = packages.toMap())

        override fun set(
            packageName: String,
            toggle: GridToggle,
            on: Boolean,
        ) {
            val now = packages[packageName].orEmpty()
            packages[packageName] = if (on) now + toggle else now - toggle
        }
    }

    private val grid = MemoryGrid()

    private fun sync(policy: AppPolicy? = AppPolicy(nonSensitive = setOf(notes))) =
        GridSync(
            api = { ApiClient(OkHttpClient(), server.url("/").toString().trimEnd('/')) },
            accessToken = { "token-1" },
            deviceId = { device },
            mapping = { mapping },
            grid = grid,
            policy = { policy },
        )

    private fun ok(
        code: Int = 200,
        body: String = "{}",
    ) = server.enqueue(MockResponse().setResponseCode(code).setBody(body))

    @Test
    fun `turning a toggle on records the user's grant for this device and that app only`() {
        grid.packages[notes] = setOf(GridToggle.SCREEN_READ)
        ok(body = listing)
        ok(201)
        val result = sync().set(notes, GridToggle.UI_INTERACTION, true)
        assertEquals(GridSync.Result.InSync, result)
        assertEquals("GET", server.takeRequest().method)
        val post = server.takeRequest()
        assertEquals("/api/v1/capabilities", post.path)
        assertEquals("Bearer token-1", post.getHeader("Authorization"))
        val body = ContractJson.parseToJsonElement(post.body.readUtf8()).jsonObject
        assertEquals("device.ui_control", body.getValue("capability").jsonPrimitive.content)
        assertEquals("device", body.getValue("scope_type").jsonPrimitive.content)
        assertEquals(device, body.getValue("scope_id").jsonPrimitive.content)
        assertEquals(
            notes,
            body
                .getValue("resource_scope")
                .jsonObject
                .getValue("package_name")
                .jsonPrimitive.content,
        )
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `turning everything off revokes only the grid's own grants`() {
        grid.packages[notes] = setOf(GridToggle.SCREEN_READ)
        ok(body = listing)
        server.enqueue(MockResponse().setResponseCode(204))
        ok(404, "{}") // already gone counts as revoked
        assertEquals(GridSync.Result.InSync, sync().set(notes, GridToggle.SCREEN_READ, false))
        server.takeRequest()
        val deletes = listOf(server.takeRequest(), server.takeRequest())
        assertTrue(deletes.all { it.method == "DELETE" })
        assertEquals(
            listOf("c001", "c002"),
            deletes.map { it.path!!.takeLast(4) },
        )
        assertEquals(3, server.requestCount)
    }

    @Test
    fun `offline, the local refusal still applies at once and the sync is pending`() {
        grid.packages[notes] = setOf(GridToggle.SCREEN_READ)
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        val result = sync().set(notes, GridToggle.SCREEN_READ, false)
        assertTrue(result is GridSync.Result.Pending)
        assertFalse(GridToggle.SCREEN_READ in grid.state().packages[notes].orEmpty())
    }

    @Test
    fun `a listing that does not parse strictly changes nothing server-side`() {
        grid.packages[notes] = emptySet()
        ok(body = listing.replace("\"scope_type\"", "\"scope_kind\""))
        assertTrue(sync().sync() is GridSync.Result.Pending)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `a refused grant leaves the sync pending`() {
        grid.packages[notes] = setOf(GridToggle.SCREEN_READ, GridToggle.UI_INTERACTION)
        ok(body = listing)
        ok(403, """{"error":{"code":"unauthorized","message":"not permitted"}}""")
        assertTrue(sync().sync() is GridSync.Result.Pending)
    }

    @Test
    fun `an unclassified app never gets a UI grant asked for`() {
        grid.packages[notes] = setOf(GridToggle.SCREEN_READ, GridToggle.UI_INTERACTION)
        ok(body = listing)
        assertEquals(GridSync.Result.InSync, sync(policy = null).sync())
        assertEquals(1, server.requestCount) // read grants already exist; nothing else asked for
    }
}
