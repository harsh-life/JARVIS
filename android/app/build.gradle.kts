import org.gradle.process.ExecOperations
import javax.inject.Inject

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

val sharedAndroidDir: File by extra
val characterAssetsDir: File by extra

// docs/23 §5.1: the shared mapping table is bundled from its one location in
// the repository (shared/android/), never copied into android/. The client
// verifies its digest at startup and refuses every operation if it does not
// match.
val bundleSharedMapping by tasks.registering(Copy::class) {
    from(sharedAndroidDir.resolve("device_mapping.json"))
    into(layout.buildDirectory.dir("generated/sharedAssets"))
}

// OD-CHAR-1: the character profiles and their approved artwork, bundled
// unchanged from their one location (assets/character/) under the APK's
// `character/` assets — never copied into android/. A Sync, not a Copy, so a
// file removed or renamed there leaves the APK too.
val bundleCharacterAssets by tasks.registering(Sync::class) {
    from(characterAssetsDir) {
        include("presentation.json", "*/profile.json", "*/*.png", "*/*.webp")
        // The animated character (docs/CHARACTER_RIG.md): the rig project's exports, and the
        // intensity profile beside them (optional at run time).
        include(
            "rig/JarvisCharacter.skel",
            "rig/JarvisCharacter.atlas",
            "rig/JarvisCharacter.contract.json",
            "rig/JarvisCharacter.intensity.json",
        )
    }
    into(layout.buildDirectory.dir("generated/characterAssets/character"))
}

// The character player's native library (docs/CHARACTER_RIG.md): android/rig-native, the vendored
// Rigby2D runtime and its JNI bridge, built with cargo for each ABI (the NDK's clang links it), and
// once for the build machine so the JVM unit tests run the real library. -Pjarvis.rig.native=false
// builds without it: the app then shows the static images (RigNative.available is false).
val rigNative = providers.gradleProperty("jarvis.rig.native").map { it.toBoolean() }.getOrElse(true)
val rigNativeDir = rootDir.resolve("rig-native")
val rigCargo = providers.gradleProperty("jarvis.rig.cargo").getOrElse("cargo")
val rigNdkVersion = "27.2.12479018"
val rigMinApi = 24
val rigAbis =
    mapOf(
        "arm64-v8a" to ("aarch64-linux-android" to "aarch64-linux-android"),
        "armeabi-v7a" to ("armv7-linux-androideabi" to "armv7a-linux-androideabi"),
        "x86_64" to ("x86_64-linux-android" to "x86_64-linux-android"),
    )
val rigHostLibrary =
    rigNativeDir.resolve("target/release").resolve(
        if (System.getProperty("os.name").startsWith("Mac")) "libjarvis_rig.dylib" else "libjarvis_rig.so",
    )

/** Runs cargo for the rig-native workspace (inputs: its sources and lockfile). */
abstract class CargoTask
    @Inject
    constructor(
        private val exec: ExecOperations,
    ) : DefaultTask() {
        @get:Input abstract val cargo: Property<String>

        @get:Input abstract val arguments: ListProperty<String>

        @get:Input abstract val environment: MapProperty<String, String>

        @get:InputFiles
        @get:PathSensitive(PathSensitivity.RELATIVE)
        abstract val sources: ConfigurableFileCollection

        @get:Internal abstract val workspace: DirectoryProperty

        @get:OutputFiles abstract val products: ConfigurableFileCollection

        @TaskAction
        fun run() {
            exec.exec {
                workingDir = workspace.get().asFile
                executable = cargo.get()
                args(arguments.get())
                environment(this@CargoTask.environment.get())
            }
        }
    }

val rigSources =
    files(
        rigNativeDir.resolve("Cargo.toml"),
        rigNativeDir.resolve("Cargo.lock"),
        fileTree(rigNativeDir.resolve("rigby-runtime")) { include("Cargo.toml", "src/**") },
        fileTree(rigNativeDir.resolve("jarvis-rig")) { include("Cargo.toml", "src/**") },
    )
val rigJniDir = layout.buildDirectory.dir("generated/rigJniLibs")

val buildRigNativeAbis =
    rigAbis.map { (abi, triples) ->
        val (rustTarget, clangTarget) = triples
        tasks.register<CargoTask>("buildRigNative_${abi.replace('-', '_')}") {
            onlyIf { rigNative }
            cargo.set(rigCargo)
            arguments.set(listOf("build", "--release", "--locked", "-p", "jarvis-rig", "--target", rustTarget))
            sources.from(rigSources)
            workspace.set(rigNativeDir)
            products.from(rigNativeDir.resolve("target/$rustTarget/release/libjarvis_rig.so"))
            environment.set(
                provider {
                    val ndk = android.ndkDirectory
                    val host = if (System.getProperty("os.name").startsWith("Mac")) "darwin-x86_64" else "linux-x86_64"
                    val clang = ndk.resolve("toolchains/llvm/prebuilt/$host/bin/$clangTarget$rigMinApi-clang")
                    check(clang.exists()) {
                        "NDK $rigNdkVersion is needed to build the character player: $clang (or -Pjarvis.rig.native=false)"
                    }
                    val key = rustTarget.uppercase().replace('-', '_')
                    mapOf("CARGO_TARGET_${key}_LINKER" to clang.absolutePath, "CC_$rustTarget" to clang.absolutePath)
                },
            )
        }
    }

val buildRigNative by tasks.registering(Sync::class) {
    onlyIf { rigNative }
    for ((i, abi) in rigAbis.keys.withIndex()) {
        from(buildRigNativeAbis[i]) { into(abi) }
    }
    into(rigJniDir)
}

val buildRigNativeHost by tasks.registering(CargoTask::class) {
    onlyIf { rigNative }
    cargo.set(rigCargo)
    arguments.set(listOf("build", "--release", "--locked", "-p", "jarvis-rig"))
    environment.set(emptyMap())
    sources.from(rigSources)
    workspace.set(rigNativeDir)
    products.from(rigHostLibrary)
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
        // docs/23 §3: the App Link host — the server's stable public hostname,
        // set per deployment (`-Pjarvis.appLinkHost=jarvis.example.com`). A
        // hostname is not a secret. The default can never verify, so a build
        // without it simply has no working browser return.
        manifestPlaceholders["appLinkHost"] =
            providers.gradleProperty("jarvis.appLinkHost").getOrElse("app-link-host.invalid")
    }

    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/sharedAssets"))
    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/characterAssets"))
    if (rigNative) {
        ndkVersion = rigNdkVersion
        sourceSets["main"].jniLibs.srcDir(rigJniDir)
    }

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
        aidl = true
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

tasks.named("preBuild") { dependsOn(bundleSharedMapping, bundleCharacterAssets) }
tasks
    .matching {
        it.name.startsWith("merge") && it.name.endsWith("JniLibFolders")
    }.configureEach { dependsOn(buildRigNative) }

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
    // Unit tests read the shared contract artifacts from their one location.
    systemProperty("jarvis.shared.dir", sharedAndroidDir.absolutePath)
    inputs.dir(sharedAndroidDir)
    // ...and the character artwork (OD-CHAR-1), to hold the APK's copy to it.
    systemProperty("jarvis.character.dir", characterAssetsDir.absolutePath)
    inputs.dir(characterAssetsDir)
    // The character player's native library, built for this machine (NativeRigRuntimeTest).
    if (rigNative) {
        dependsOn(buildRigNativeHost)
        systemProperty("jarvis.rig.native.lib", rigHostLibrary.absolutePath)
        systemProperty("jarvis.rig.native.required", "true")
    }
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
    implementation(libs.okhttp)
    implementation(libs.tink.android)
    // docs/23 §6 level 3: the bundled (on-device) Latin model — no model
    // download, no image upload.
    implementation(libs.mlkit.text.recognition)
    // docs/23 §5.3: Shizuku, on demand, for typed primitives only (AIDL in
    // src/main/aidl). Never a shell.
    // docs/23 §3 step-up: the platform's biometric / device-credential prompt.
    implementation(libs.androidx.biometric)
    implementation(libs.shizuku.api)
    implementation(libs.shizuku.provider)
    // docs/23 §4 push wake — optional and OFF by default. The SDK is linked
    // but never initialized unless the user's server offers FCM *and* the
    // user turns it on: no automatic initialization (the init provider is
    // removed in the manifest), no auto-registration, and the receiving
    // service is disabled until then. Only push/FirebasePushClient.kt and
    // push/JarvisMessagingService.kt touch it.
    implementation(libs.firebase.messaging)

    coreLibraryDesugaring(libs.desugar.jdk.libs)

    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    robolectricRuntime(libs.robolectric.android.all)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
    // Compose UI tests on Robolectric (the character's rendering and sizing).
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test.junit4)

    androidTestImplementation(platform(libs.compose.bom))
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.tooling)
    debugImplementation(libs.compose.ui.test.manifest)
}
