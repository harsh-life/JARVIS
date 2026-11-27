import org.gradle.process.ExecOperations
import java.awt.AlphaComposite
import java.awt.RenderingHints
import java.awt.geom.Ellipse2D
import java.awt.geom.RoundRectangle2D
import java.awt.image.BufferedImage
import javax.imageio.ImageIO
import javax.inject.Inject
import java.awt.Color as AwtColor

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

// The brand mark: the satisfied face (assets/character/default/satisfied.png, the owner's original)
// is JARVIS's logo. Its launcher icons (with their themed-icon layer) and notification icon are
// rendered from that one file at build time, at every density, so the artwork is never copied,
// redrawn or committed twice.
// The square below frames the hood, face and raised fists (source pixels; it may run past the
// image, where the sticker is transparent). MainActivity's header badge draws the part of it a
// launcher shows, from the bundled original (the generated values/brand_mark.xml resources).
val brandSource = characterAssetsDir.resolve("default/satisfied.png")
val brandFrame = listOf(-19, 56, 880) // left, top, side
val brandStatusFrame = listOf(150, 230, 545) // the head alone, for the status bar

/** Renders the brand mark's launcher and notification icons from the satisfied-face original. */
abstract class BrandIcons : DefaultTask() {
    @get:InputFile
    @get:PathSensitive(PathSensitivity.NONE)
    abstract val source: RegularFileProperty

    @get:Input abstract val frame: ListProperty<Int>

    @get:Input abstract val statusFrame: ListProperty<Int>

    @get:Input abstract val backgroundRgb: Property<Int>

    /** The artwork's path inside the APK's assets, for the header badge. */
    @get:Input abstract val assetPath: Property<String>

    @get:OutputDirectory abstract val outputDir: DirectoryProperty

    @TaskAction
    fun render() {
        System.setProperty("java.awt.headless", "true")
        val src = ImageIO.read(source.get().asFile) ?: error("unreadable: ${source.get()}")
        check(src.colorModel.hasAlpha()) { "the brand artwork must keep its transparent surroundings" }
        val out = outputDir.get().asFile
        out.deleteRecursively()
        val (l, t, s) = frame.get()
        val art = crop(src, l, t, s)
        val (hl, ht, hs) = statusFrame.get()
        val status = statusSilhouette(crop(src, hl, ht, hs))
        val monochrome = statusSilhouette(art)
        val densities = listOf("mdpi" to 1.0, "hdpi" to 1.5, "xhdpi" to 2.0, "xxhdpi" to 3.0, "xxxhdpi" to 4.0)
        for ((bucket, scale) in densities) {
            // Adaptive icon (API 26+): a 108dp foreground layer over the background colour.
            val fg = scaleTo(art, (108 * scale).toInt())
            write(fg, out.resolve("mipmap-$bucket/ic_launcher_foreground.png"))
            // Its themed-icon layer (API 33+): the same frame in one colour, which the launcher tints.
            write(scaleTo(monochrome, (108 * scale).toInt()), out.resolve("mipmap-$bucket/ic_launcher_monochrome.png"))
            // Legacy icons (API 24-25): the adaptive icon's centre, on its background, masked with a margin round the hood.
            val legacy = (48 * scale).toInt()
            val inner = scaleTo(crop(art, s / 8, s / 8, s * 3 / 4), legacy)
            write(legacyIcon(inner, roundRect = true), out.resolve("mipmap-$bucket/ic_launcher.png"))
            write(legacyIcon(inner, roundRect = false), out.resolve("mipmap-$bucket/ic_launcher_round.png"))
            // Status-bar icon: white, alpha only (the platform tints it).
            write(scaleTo(status, (24 * scale).toInt()), out.resolve("drawable-$bucket/ic_stat_jarvis.png"))
        }
        // The in-app header's brand badge shows what a launcher shows of the icon: the adaptive
        // layer's central two thirds (72 of 108dp), i.e. the middle of the frame above. Emitted as
        // generated resources beside the icons (not BuildConfig, which REPO-T2 keeps empty).
        val values = out.resolve("values/brand_mark.xml")
        values.parentFile.mkdirs()
        values.writeText(
            """
            |<?xml version="1.0" encoding="utf-8"?>
            |<!-- Generated by renderBrandIcons (android/app/build.gradle.kts). Do not edit. -->
            |<resources>
            |    <string name="brand_asset" translatable="false">${assetPath.get()}</string>
            |    <integer name="brand_frame_left">${l + s / 6}</integer>
            |    <integer name="brand_frame_top">${t + s / 6}</integer>
            |    <integer name="brand_frame_side">${s * 2 / 3}</integer>
            |</resources>
            |
            """.trimMargin(),
        )
    }

    private fun crop(
        img: BufferedImage,
        left: Int,
        top: Int,
        side: Int,
    ): BufferedImage {
        val o = BufferedImage(side, side, BufferedImage.TYPE_INT_ARGB_PRE)
        o.createGraphics().apply { drawImage(img, -left, -top, null) }.dispose()
        return o
    }

    /** Halve with bicubic filtering until close, then one last step: clean edges at every size. */
    private fun scaleTo(
        img: BufferedImage,
        px: Int,
    ): BufferedImage {
        var cur = img
        var side = img.width
        while (side / 2 >= px) {
            side /= 2
            cur = resample(cur, side)
        }
        return if (side == px) cur else resample(cur, px)
    }

    private fun resample(
        img: BufferedImage,
        px: Int,
    ): BufferedImage {
        val o = BufferedImage(px, px, BufferedImage.TYPE_INT_ARGB_PRE)
        o
            .createGraphics()
            .apply {
                setRenderingHint(RenderingHints.KEY_INTERPOLATION, RenderingHints.VALUE_INTERPOLATION_BICUBIC)
                setRenderingHint(RenderingHints.KEY_RENDERING, RenderingHints.VALUE_RENDER_QUALITY)
                drawImage(img, 0, 0, px, px, null)
            }.dispose()
        return o
    }

    private fun legacyIcon(
        art: BufferedImage,
        roundRect: Boolean,
    ): BufferedImage {
        val px = art.width
        val o = BufferedImage(px, px, BufferedImage.TYPE_INT_ARGB_PRE)
        o
            .createGraphics()
            .apply {
                setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON)
                val d = px.toDouble()
                fill(
                    if (roundRect) {
                        RoundRectangle2D.Double(0.0, 0.0, d, d, d * 0.35, d * 0.35)
                    } else {
                        Ellipse2D.Double(0.0, 0.0, d, d)
                    },
                )
                // The background, then the art, only inside the (anti-aliased) shape just drawn.
                composite = AlphaComposite.SrcIn
                color = AwtColor(this@BrandIcons.backgroundRgb.get())
                fillRect(0, 0, px, px)
                composite = AlphaComposite.SrcAtop
                drawImage(art, 0, 0, null)
            }.dispose()
        return o
    }

    /**
     * The artwork's own tones as a white mask: its light areas (fur, face, sticker edge) stay,
     * its dark line work (outlines, hair, eyes, smile) is cut out, so the face still reads
     * when the platform tints the icon one colour.
     */
    private fun statusSilhouette(img: BufferedImage): BufferedImage {
        val o = BufferedImage(img.width, img.height, BufferedImage.TYPE_INT_ARGB_PRE)
        for (y in 0 until img.height) {
            for (x in 0 until img.width) {
                val p = img.getRGB(x, y)
                val a = p ushr 24
                val lum = (0.2126 * (p shr 16 and 255) + 0.7152 * (p shr 8 and 255) + 0.0722 * (p and 255)) / 255
                val keep = ((lum - 0.25) / 0.35).coerceIn(0.0, 1.0)
                o.setRGB(x, y, (Math.round(a * keep).toInt() shl 24) or 0xFFFFFF)
            }
        }
        return o
    }

    private fun write(
        img: BufferedImage,
        file: File,
    ) {
        file.parentFile.mkdirs()
        val o = BufferedImage(img.width, img.height, BufferedImage.TYPE_INT_ARGB)
        o.createGraphics().apply { drawImage(img, 0, 0, null) }.dispose()
        check(ImageIO.write(o, "png", file)) { "could not write $file" }
    }
}

val renderBrandIcons by tasks.registering(BrandIcons::class) {
    source.set(brandSource)
    frame.set(brandFrame)
    statusFrame.set(brandStatusFrame)
    // res/values/colors.xml ic_launcher_background, the adaptive icon's background layer.
    backgroundRgb.set(0x0E1424)
    // bundleCharacterAssets places assets/character/ at the APK's assets/character/.
    assetPath.set("character/${brandSource.relativeTo(characterAssetsDir).invariantSeparatorsPath}")
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

// The brand icons join the app's resources as a generated source, so every task that reads
// resources (merge, lint, unit tests) depends on renderBrandIcons.
androidComponents {
    onVariants { variant ->
        variant.sources.res?.addGeneratedSourceDirectory(renderBrandIcons, BrandIcons::outputDir)
    }
}
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
