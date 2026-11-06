package com.hypermind.jarvis.presentation.character.rig

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.IOException
import java.io.InputStream

/** A loaded animated character: its player, atlas page and frame. [close] releases all of it. */
class LoadedRig(
    val player: CharacterAnimationPlayer,
    val page: Bitmap,
    val info: RigInfo,
) : AutoCloseable {
    override fun close() {
        player.close()
        if (!page.isRecycled) page.recycle()
    }
}

/** Loads a rig, or null when there is none to show (the screen then keeps the static images). */
fun interface RigLoader {
    fun load(
        open: (String) -> InputStream,
        reducedMotion: Boolean,
    ): LoadedRig?
}

/**
 * The animated character the APK bundles under `character/rig/` (from `assets/character/rig/`,
 * the rig project's exports, unchanged). Every failure — a missing or malformed file, a contract
 * the rig does not satisfy, no native runtime — is "no rig", never an exception; whatever was
 * opened before the failure is released.
 *
 * The intensity profile ([INTENSITY]) is optional: missing, invalid or refused, the rig still
 * plays — canonically, every intensity at gain 1. In reduced motion it is not applied at all.
 */
object RigAssets {
    const val DIR_NAME = "rig"
    const val ROOT = "character/$DIR_NAME"
    const val SKELETON = "JarvisCharacter.skel"
    const val ATLAS = "JarvisCharacter.atlas"
    const val PAGE = "JarvisCharacter.png"
    const val CONTRACT = "JarvisCharacter.contract.json"
    const val INTENSITY = "JarvisCharacter.intensity.json"

    /** The bundled rig on the Rigby2D runtime. */
    val Bundled = RigLoader { open, reducedMotion -> load(open, reducedMotion) }

    fun load(
        open: (String) -> InputStream,
        reducedMotion: Boolean,
        runtime: (ByteArray, ByteArray) -> Result<RigRuntime> = { s, a -> NativeRigRuntime.open(s, a) },
    ): LoadedRig? {
        val files = Files.from(open) ?: return null
        // The contract first: a malformed one never opens a native rig.
        val contract = RigContract.parse(files.contract.toString(Charsets.UTF_8)) ?: return null
        val rig = runtime(files.skeleton, files.atlas).getOrNull() ?: return null
        val profile =
            if (reducedMotion) {
                null
            } else {
                read(open, INTENSITY)
                    ?.toString(Charsets.UTF_8)
                    ?.let(RigIntensityProfile::parse)
                    ?.let { RigShaping.configure(rig, contract, it) }
            }
        val player = RigCharacterPlayer.create(rig, contract, reducedMotion, profile) ?: return null
        val page =
            decode(files.page)?.takeIf { fits(it, rig.info) } ?: run {
                player.close()
                return null
            }
        return LoadedRig(player, page, rig.info)
    }

    private class Files(
        val contract: ByteArray,
        val skeleton: ByteArray,
        val atlas: ByteArray,
        val page: ByteArray,
    ) {
        companion object {
            fun from(open: (String) -> InputStream): Files? {
                val bytes = listOf(CONTRACT, SKELETON, ATLAS, PAGE).map { read(open, it) ?: return null }
                return Files(contract = bytes[0], skeleton = bytes[1], atlas = bytes[2], page = bytes[3])
            }
        }
    }

    /** The page is drawn by page-pixel coordinates, so it must be exactly the atlas's size; else it is released. */
    private fun fits(
        page: Bitmap,
        info: RigInfo,
    ): Boolean {
        val ok = page.width.toFloat() == info.pageWidth && page.height.toFloat() == info.pageHeight
        if (!ok) page.recycle()
        return ok
    }

    private fun read(
        open: (String) -> InputStream,
        name: String,
    ): ByteArray? =
        try {
            open("$ROOT/$name").use { it.readBytes() }
        } catch (ignored: IOException) {
            null
        }

    private fun decode(bytes: ByteArray): Bitmap? {
        // Unscaled, so texture coordinates in page pixels map one to one; premultiplied (the
        // default), which is what drawing with a bitmap shader expects.
        val options = BitmapFactory.Options().apply { inScaled = false }
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
    }
}
