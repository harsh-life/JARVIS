package com.hypermind.jarvis.presentation.character.rig

import java.io.File

/** The rig files the APK bundles, read from their one location (assets/character/rig/). */
object RigAssetsFixture {
    val dir: File =
        File(requireNotNull(System.getProperty("jarvis.character.dir")) { "jarvis.character.dir is set by the build" })
            .resolve(RigAssets.DIR_NAME)

    fun bytes(name: String): ByteArray = dir.resolve(name).readBytes()

    fun text(name: String): String = bytes(name).toString(Charsets.UTF_8)

    fun contract(): RigContract = requireNotNull(RigContract.parse(text(RigAssets.CONTRACT)))

    /** Opens `character/rig/<name>` the way the app's AssetManager would. */
    fun open(path: String): java.io.InputStream {
        require(path.startsWith(RigAssets.ROOT + "/")) { path }
        return dir.resolve(path.removePrefix(RigAssets.ROOT + "/")).inputStream()
    }
}
