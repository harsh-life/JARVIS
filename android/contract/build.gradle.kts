// Pure Kotlin/JVM: the wire contract, the shared mapping table and (docs/23
// §5.2) the device-side guard. No Android API is reachable from here, so the
// security-relevant logic runs as plain JVM unit tests against the same shared
// conformance vectors the server uses.
plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
        allWarningsAsErrors.set(true)
    }
}

dependencies {
    implementation(libs.kotlinx.serialization.json)
    testImplementation(libs.junit)
}

val sharedAndroidDir: File by extra

tasks.test {
    // The tests read the shared artifacts from their one location in the repo.
    systemProperty("jarvis.shared.dir", sharedAndroidDir.absolutePath)
    inputs.dir(sharedAndroidDir)
}
