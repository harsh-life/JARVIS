package com.hypermind.jarvis.ui

import com.hypermind.jarvis.contract.AgentList
import com.hypermind.jarvis.contract.ContractJson
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import java.io.File

/** The Agents tab's rows: Run only for an active agent, and only while no task is live. */
class AgentRowsTest {
    private val list =
        AgentList.parse(
            200,
            ContractJson
                .parseToJsonElement(
                    File(requireNotNull(System.getProperty("jarvis.shared.dir")))
                        .resolve("agent_samples.json")
                        .readText(),
                ).jsonObject
                .getValue("agent_list")
                .toString(),
        )

    @Test
    fun `an active agent offers Run, a paused one does not`() {
        val rows = AgentRow.of(list, canRun = true)
        assertEquals(listOf(true, false), rows.map { it.runEnabled })
        assertEquals("paused", rows[1].detail)
    }

    @Test
    fun `while a task is live nothing offers Run`() {
        assertEquals(listOf(false, false), AgentRow.of(list, canRun = false).map { it.runEnabled })
    }

    @Test
    fun `a failed list has no rows, and a row's name never reaches toString`() {
        assertEquals(emptyList<AgentRow>(), AgentRow.of(AgentList.Failed("unreachable"), canRun = true))
        assertFalse("advisory" in AgentRow.of(list, canRun = true)[0].toString())
    }
}
