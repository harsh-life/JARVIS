plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

val sharedAndroidDir: File by extra

// docs/23 §5.1: the shared mapping table is bundled from its one location in
// the repository (shared/android/), never copied into android/. The client
// verifies its digest at startup and refuses every operation if it does not
// match.
val bundleSharedMapping by tasks.registering(Copy::class) {
    from(sharedAndroidDir.resolve("device_mapping.json"))
    into(layout.buildDirectory.dir("generated/sharedAssets"))
}

android {
    namespace = "com.hypermind.jarvis"
    compileSdk = 35

    defaultConfig {
        // Deliberately not the donor's `com.hypermind.edge`: an upgrade over
        // that app would inherit its plaintext SharedPreferences (a Groq key
        // and a gateway bearer token). A new identity starts from nothing.
        applicationId = "com.hypermind.jarvis"
        // Kept from the donor: budget devices skew old. Features that need a
        // newer platform are gated by API level at runtime, never assumed.
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/sharedAssets"))

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            // No signing config is committed: a release is signed by the
            // operator's own key, supplied outside the repository.
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        isCoreLibraryDesugaringEnabled = true
    }

    kotlinOptions {
        jvmTarget = "17"
        allWarningsAsErrors = true
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }

    testOptions {
        unitTests.isIncludeAndroidResources = true
    }

    lint {
        abortOnError = true
        warningsAsErrors = false
        checkReleaseBuilds = true
    }
}

tasks.named("preBuild") { dependsOn(bundleSharedMapping) }

// Robolectric's Android framework jar is resolved by Gradle (with its retries
// and cache) instead of Robolectric's own downloader at test time, and the
// tests run offline against it.
val robolectricRuntime: Configuration by configurations.creating
val robolectricJarDir = layout.buildDirectory.dir("robolectric-jars")
val robolectricJars by tasks.registering(Copy::class) {
    from(robolectricRuntime)
    into(robolectricJarDir)
}
tasks.withType<Test>().configureEach {
    dependsOn(robolectricJars)
    systemProperty("robolectric.offline", "true")
    systemProperty("robolectric.dependency.dir", robolectricJarDir.get().asFile.absolutePath)
}

dependencies {
    implementation(project(":contract"))

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)

    coreLibraryDesugaring(libs.desugar.jdk.libs)

    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    robolectricRuntime(libs.robolectric.android.all)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.kotlinx.coroutines.test)

    androidTestImplementation(platform(libs.compose.bom))
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.tooling)
    debugImplementation(libs.compose.ui.test.manifest)
}
