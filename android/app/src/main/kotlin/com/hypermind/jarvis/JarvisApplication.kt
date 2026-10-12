package com.hypermind.jarvis

import android.app.Application
import com.hypermind.jarvis.contract.MappingAsset
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.presentation.character.CharacterAssets
import com.hypermind.jarvis.presentation.character.CharacterPresentation

class JarvisApplication : Application() {
    lateinit var mapping: MappingState
        private set

    val graph: AppGraph by lazy { AppGraph(this, mapping) }

    /**
     * OD-CHAR-1: the character's expression — presentation only. Kept apart
     * from [graph] (nothing there reads it) and from the mapping table, so the
     * character shows whatever state the device is in.
     */
    val character: CharacterPresentation by lazy { CharacterPresentation(CharacterAssets.profile(assets)) }

    override fun onCreate() {
        super.onCreate()
        mapping = MappingAsset.load(this)
    }
}
