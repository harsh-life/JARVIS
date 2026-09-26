package com.hypermind.jarvis

import android.app.Application
import com.hypermind.jarvis.contract.MappingAsset
import com.hypermind.jarvis.contract.MappingState

class JarvisApplication : Application() {
    lateinit var mapping: MappingState
        private set

    override fun onCreate() {
        super.onCreate()
        mapping = MappingAsset.load(this)
    }
}
