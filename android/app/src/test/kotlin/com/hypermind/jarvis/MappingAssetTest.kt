package com.hypermind.jarvis

import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.contract.MappingAsset
import com.hypermind.jarvis.contract.MappingState
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class MappingAssetTest {
    @Test
    fun `the packaged app carries the verified shared mapping`() {
        val state = MappingAsset.load(ApplicationProvider.getApplicationContext())
        assertTrue("$state", state is MappingState.Valid)
    }
}
