// The Android client (docs/23). It shares exactly two things with the server
// (16 §4, REPO-T3): shared/schemas' wire contract, mirrored in :contract, and
// the versioned mapping artifact shared/android/device_mapping.json, bundled at
// build time from its one location — never copied into this tree.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.ktlint) apply false
    alias(libs.plugins.detekt) apply false
}

val sharedDir = rootDir.resolve("../shared/android")
val ktlintVersion: String = libs.versions.ktlint.engine.get()

subprojects {
    apply(plugin = "org.jlleitschuh.gradle.ktlint")
    apply(plugin = "io.gitlab.arturbosch.detekt")

    extensions.configure<org.jlleitschuh.gradle.ktlint.KtlintExtension> {
        version.set(ktlintVersion)
        android.set(true)
        ignoreFailures.set(false)
    }

    extensions.configure<io.gitlab.arturbosch.detekt.extensions.DetektExtension> {
        buildUponDefaultConfig = true
        allRules = false
        config.setFrom(rootProject.file("config/detekt/detekt.yml"))
        parallel = true
    }

    // detekt 1.23.7 is built against Kotlin 2.0.10; keep its own classpath on that
    // compiler rather than the project's 2.0 one.
    configurations.matching { it.name == "detekt" }.configureEach {
        resolutionStrategy.eachDependency {
            if (requested.group == "org.jetbrains.kotlin") useVersion("2.0.10")
        }
    }

    extra["sharedAndroidDir"] = sharedDir
}
