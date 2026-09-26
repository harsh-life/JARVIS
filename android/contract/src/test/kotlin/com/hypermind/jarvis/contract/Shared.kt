package com.hypermind.jarvis.contract

import java.io.File

/** The repository's `shared/android/` directory, passed in by Gradle. */
object Shared {
    val dir: File =
        File(
            requireNotNull(System.getProperty("jarvis.shared.dir")) {
                "run through Gradle: jarvis.shared.dir is not set"
            },
        )

    fun read(name: String): String = dir.resolve(name).readText(Charsets.US_ASCII)
}
