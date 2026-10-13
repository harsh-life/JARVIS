package com.hypermind.jarvis

import android.app.Application
import android.os.SystemClock
import com.hypermind.jarvis.contract.MappingAsset
import com.hypermind.jarvis.contract.MappingState
import com.hypermind.jarvis.presentation.character.CharacterAssets
import com.hypermind.jarvis.presentation.character.CharacterPresentation
import kotlinx.coroutines.MainScope

class JarvisApplication : Application() {
    lateinit var mapping: MappingState
        private set

    val graph: AppGraph by lazy { AppGraph(this, mapping) }

    /**
     * OD-CHAR-1/2: the character's expression — presentation only, selected
     * on this device from the current interaction and verified events. Kept
     * apart from [graph] (nothing there reads it) and from the mapping table,
     * so the character shows whatever state the device is in.
     */
    val character: CharacterPresentation by lazy {
        val settings = CharacterAssets.settings(assets)
        CharacterPresentation(
            scope = MainScope(),
            clock = SystemClock::elapsedRealtime,
            profile = CharacterAssets.profile(assets, settings.profileId),
            settings = settings,
        )
    }

    override fun onCreate() {
        super.onCreate()
        mapping = MappingAsset.load(this)
    }
}
