package com.hypermind.jarvis.perception

import com.hypermind.jarvis.contract.AppMetadata
import com.hypermind.jarvis.contract.Bounds
import com.hypermind.jarvis.contract.ScreenReadResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** docs/23 §6 level 1 — bounded, redacted on the device (ANDC-T6). */
class TreeExtractorTest {
    private val extractor = TreeExtractor()

    @Test
    fun `a password field keeps its shape and loses its content`() {
        val tree =
            layout(
                FakeNode(text = "Sign in"),
                FakeNode(
                    className = "android.widget.EditText",
                    text = "hunter2",
                    contentDescription = "PIN 1234",
                    password = true,
                    editable = true,
                    viewId = "app:id/pin",
                ),
            )
        val nodes = extractor.extract(tree).nodes
        val pin = nodes.single { it.password }
        assertNull(pin.text)
        assertNull(pin.contentDescription)
        assertEquals("app:id/pin", pin.viewId)
        assertTrue(pin.editable)
        assertFalse(nodes.any { it.text == "hunter2" || it.contentDescription == "PIN 1234" })
    }

    @Test
    fun `ids are pre-order so every parent precedes its children`() {
        val tree = layout(layout(FakeNode(text = "a"), FakeNode(text = "b")), FakeNode(text = "c"))
        val nodes = extractor.extract(tree).nodes
        assertEquals(listOf(null, 0, 1, 1, 0), nodes.map { it.parent })
        assertEquals(listOf(null, null, "a", "b", "c"), nodes.map { it.text })
        // …which is exactly what the shared contract requires.
        ScreenReadResult(AppMetadata("com.example.notes"), nodes)
    }

    @Test
    fun `invisible subtrees are not read`() {
        val tree =
            layout(FakeNode(text = "shown"), FakeNode(visible = false, children = listOf(FakeNode(text = "hidden"))))
        assertEquals(listOf(null, "shown"), extractor.extract(tree).nodes.map { it.text })
    }

    @Test
    fun `the node count is bounded and truncation is reported`() {
        val tree = layout(*Array(Bounds.MAX_SCREEN_NODES + 50) { FakeNode(text = "n$it") })
        val extracted = extractor.extract(tree)
        assertEquals(Bounds.MAX_SCREEN_NODES, extracted.nodes.size)
        assertTrue(extracted.truncated)
    }

    @Test
    fun `depth is bounded without recursion`() {
        var deep = FakeNode(text = "leaf")
        repeat(5_000) { deep = FakeNode(className = "v", children = listOf(deep)) }
        val extracted = TreeExtractor(maxDepth = 10).extract(deep)
        assertEquals(11, extracted.nodes.size)
        assertTrue(extracted.truncated)
    }

    @Test
    fun `text is clipped to the contract bound without splitting a surrogate pair`() {
        val emoji = "😀"
        val long = "a" + emoji.repeat(Bounds.MAX_NODE_TEXT)
        val clipped = TreeExtractor.clip(long, Bounds.MAX_NODE_TEXT)!!
        assertEquals(Bounds.MAX_NODE_TEXT, clipped.codePointCount(0, clipped.length))
        assertFalse(Character.isHighSurrogate(clipped.last()))
        assertNull(TreeExtractor.clip("   ", 10))
    }

    @Test
    fun `a tree with no readable text is not informative`() {
        assertFalse(extractor.extract(layout(layout(), FakeNode(className = "android.view.View"))).informative)
        assertTrue(extractor.extract(layout(FakeNode(contentDescription = "Send"))).informative)
    }
}
