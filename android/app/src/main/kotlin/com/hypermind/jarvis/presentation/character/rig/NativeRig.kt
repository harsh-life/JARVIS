package com.hypermind.jarvis.presentation.character.rig

/**
 * The JNI surface of `libjarvis_rig` (android/rig-native: the vendored Rigby2D runtime and its
 * bridge). Only primitive arrays cross it; it keeps no reference to any Java object.
 *
 * The library is loaded once, lazily. Without it (a build made with `-Pjarvis.rig.native=false`,
 * an ABI it was not built for, or a JVM unit test) [available] is false and the character keeps
 * its static images.
 */
internal object RigNative {
    @Volatile
    private var loaded: Boolean? = null

    /** Why loading failed, if it did. */
    @Volatile
    var loadError: String? = null
        private set

    val available: Boolean
        get() = loaded ?: load { System.loadLibrary(LIBRARY) }

    /**
     * JVM unit tests only: loads the library the way [how] does (the test loads the host build by
     * path) instead of from the APK. Production code never loads native code by path.
     */
    fun loadForTests(how: () -> Unit): Boolean = load(how)

    @Synchronized
    private fun load(how: () -> Unit): Boolean {
        loaded?.let { return it }
        val ok =
            try {
                how()
                true
            } catch (e: UnsatisfiedLinkError) {
                loadError = e.message ?: "unsatisfied link"
                false
            } catch (e: SecurityException) {
                loadError = e.message ?: "not allowed"
                false
            }
        loaded = ok
        return ok
    }

    const val LIBRARY = "jarvis_rig"
    const val INFO_SIZE = 9
    const val ERROR_SIZE = 256

    @JvmStatic external fun nativeCreate(
        skeleton: ByteArray,
        atlas: ByteArray,
        error: ByteArray,
    ): Long

    @JvmStatic external fun nativeDestroy(handle: Long)

    @JvmStatic external fun nativeInfo(
        handle: Long,
        out: FloatArray,
    ): Boolean

    @JvmStatic external fun nativeAnimationIndex(
        handle: Long,
        name: ByteArray,
    ): Int

    @JvmStatic external fun nativeAnimationDuration(
        handle: Long,
        index: Int,
    ): Float

    @JvmStatic external fun nativeSetAnimation(
        handle: Long,
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean

    @JvmStatic external fun nativeAddAnimation(
        handle: Long,
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean

    @JvmStatic external fun nativeClearTrack(
        handle: Long,
        track: Int,
    )

    @JvmStatic external fun nativeTrack(
        handle: Long,
        track: Int,
        out: FloatArray,
    ): Boolean

    @JvmStatic external fun nativeUpdate(
        handle: Long,
        dt: Float,
        events: IntArray,
    ): Int

    @JvmStatic external fun nativeBuild(
        handle: Long,
        positions: FloatArray,
        texs: FloatArray,
        indices: ShortArray,
        counts: IntArray,
    ): Boolean
}

/**
 * The Rigby2D runtime behind [RigRuntime]. It owns one native rig, freed by [close]: exactly once,
 * whatever calls it, after which every other call throws (a use after release is a bug, not a
 * no-op that would hide it). Confined to its owner's thread.
 */
class NativeRigRuntime private constructor(
    private var handle: Long,
    override val info: RigInfo,
) : RigRuntime {
    private val track = FloatArray(TRACK_SIZE)

    private fun live(): Long {
        check(handle != 0L) { "the rig was released" }
        return handle
    }

    override fun animationIndex(name: String): Int =
        RigNative.nativeAnimationIndex(
            live(),
            name.toByteArray(Charsets.UTF_8),
        )

    override fun setAnimation(
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean = RigNative.nativeSetAnimation(live(), track, index, loop, mix)

    override fun addAnimation(
        track: Int,
        index: Int,
        loop: Boolean,
        mix: Float,
    ): Boolean = RigNative.nativeAddAnimation(live(), track, index, loop, mix)

    override fun clearTrack(track: Int) = RigNative.nativeClearTrack(live(), track)

    override fun current(track: Int): Int =
        if (RigNative.nativeTrack(
                live(),
                track,
                this.track,
            )
        ) {
            this.track[0].toInt()
        } else {
            -1
        }

    override fun update(
        seconds: Float,
        events: IntArray,
    ): Int = RigNative.nativeUpdate(live(), seconds, events)

    override fun build(geometry: RigGeometry): Boolean {
        val ok = RigNative.nativeBuild(live(), geometry.positions, geometry.texs, geometry.indices, geometry.counts)
        geometry.vertexCount = if (ok) geometry.counts[0] else 0
        geometry.indexCount = if (ok) geometry.counts[1] else 0
        return ok
    }

    override fun close() {
        val h = handle
        handle = 0L
        if (h != 0L) RigNative.nativeDestroy(h)
    }

    companion object {
        private const val TRACK_SIZE = 3

        /**
         * Loads a rig from skeleton (`.skel`) and atlas bytes. Fails if the native library is not
         * available or the runtime refuses the files (the message says why).
         */
        fun open(
            skeleton: ByteArray,
            atlas: ByteArray,
        ): Result<NativeRigRuntime> {
            if (!RigNative.available) {
                return Result.failure(
                    IllegalStateException("native runtime unavailable: ${RigNative.loadError}"),
                )
            }
            val error = ByteArray(RigNative.ERROR_SIZE)
            val handle = RigNative.nativeCreate(skeleton, atlas, error)
            if (handle == 0L) {
                val message = error.toString(Charsets.UTF_8).trimEnd('\u0000')
                return Result.failure(IllegalArgumentException(message.ifEmpty { "the rig did not load" }))
            }
            val out = FloatArray(RigNative.INFO_SIZE)
            if (!RigNative.nativeInfo(handle, out)) {
                RigNative.nativeDestroy(handle)
                return Result.failure(IllegalStateException("the rig did not describe itself"))
            }
            val info =
                RigInfo(
                    vertexCapacity = out[0].toInt(),
                    indexCapacity = out[1].toInt(),
                    animationCount = out[2].toInt(),
                    minX = out[3],
                    minY = out[4],
                    maxX = out[5],
                    maxY = out[6],
                    pageWidth = out[7],
                    pageHeight = out[8],
                )
            return Result.success(NativeRigRuntime(handle, info))
        }
    }
}
