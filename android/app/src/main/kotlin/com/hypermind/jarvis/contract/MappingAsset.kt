package com.hypermind.jarvis.contract

import android.content.Context

/**
 * The bundled shared mapping table (docs/23 §5.1), verified against its own
 * digest. A table that is missing or does not verify yields [Invalid], and a
 * device holding an invalid table refuses every operation — it never falls
 * back to "no checks".
 */
sealed interface MappingState {
    data class Valid(
        val mapping: DeviceMapping,
    ) : MappingState

    data class Invalid(
        val reason: String,
    ) : MappingState
}

object MappingAsset {
    const val ASSET_NAME = "device_mapping.json"

    fun load(context: Context): MappingState =
        try {
            val text = context.assets.open(ASSET_NAME).use { it.readBytes().toString(Charsets.US_ASCII) }
            MappingState.Valid(DeviceMapping.load(text))
        } catch (e: java.io.IOException) {
            MappingState.Invalid("mapping asset unreadable: ${e.javaClass.simpleName}")
        } catch (e: IllegalArgumentException) {
            MappingState.Invalid("mapping asset malformed: ${e.javaClass.simpleName}")
        } catch (e: IllegalStateException) {
            MappingState.Invalid("mapping asset rejected: ${e.message}")
        }
}
