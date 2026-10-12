package com.hypermind.jarvis.presentation.character

import android.content.res.AssetManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.IOException
import java.io.InputStream

/** One decoded character image, keyed by the asset it came from. */
class CharacterImage(
    val asset: String,
    val bitmap: Bitmap,
)

/**
 * Reads the character's profile and images from the app's assets (bundled
 * from `assets/character/` at build time). Every failure falls back: an
 * unreadable or invalid profile to [CharacterProfile.BUILT_IN], an image
 * that will not decode to the profile's fallback image, and a missing
 * fallback to nothing at all — the status line never depends on it.
 */
object CharacterAssets {
    fun profile(
        assets: AssetManager,
        profileId: String = CharacterProfile.BUILT_IN.profileId,
    ): CharacterProfile =
        try {
            val path = "${CharacterProfile.ASSET_ROOT}/$profileId/${CharacterProfile.PROFILE_FILE}"
            val text = assets.open(path).use { it.readBytes().toString(Charsets.UTF_8) }
            CharacterProfile.parse(text)?.takeIf { it.profileId == profileId } ?: CharacterProfile.BUILT_IN
        } catch (ignored: IOException) {
            CharacterProfile.BUILT_IN
        }

    /** The image for [expression], scaled down for a [targetHeightPx]-tall slot; the fallback's if it fails. */
    fun load(
        profile: CharacterProfile,
        expression: Expression,
        targetHeightPx: Int,
        open: (String) -> InputStream,
    ): CharacterImage? {
        val asset = profile.assetFor(expression)
        val fallback = profile.assetFor(profile.fallback)
        return decode(profile, asset, targetHeightPx, open)
            ?: if (asset != fallback) decode(profile, fallback, targetHeightPx, open) else null
    }

    /**
     * The largest power-of-two reduction that keeps the image at least
     * [targetHeightPx] tall — never upscaled, never decoded at full size on a
     * small slot (a 1536 px image is ~9 MB decoded).
     */
    fun sampleSize(
        heightPx: Int,
        targetHeightPx: Int,
    ): Int {
        var sample = 1
        if (heightPx <= 0 || targetHeightPx <= 0) return sample
        while (heightPx / (sample * 2) >= targetHeightPx) sample *= 2
        return sample
    }

    private fun decode(
        profile: CharacterProfile,
        asset: String,
        targetHeightPx: Int,
        open: (String) -> InputStream,
    ): CharacterImage? =
        try {
            val path = profile.assetPath(asset)
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            open(path).use { BitmapFactory.decodeStream(it, null, bounds) }
            val options = BitmapFactory.Options().apply { inSampleSize = sampleSize(bounds.outHeight, targetHeightPx) }
            open(path).use { BitmapFactory.decodeStream(it, null, options) }?.let { CharacterImage(asset, it) }
        } catch (ignored: IOException) {
            null
        }
}

/**
 * The character's on-screen height for a window of the given size (dp): a
 * share of the width, never more than a quarter of the height (so landscape
 * and split-screen keep room for the task), clamped to a range that stays
 * legible on small phones and does not dominate a tablet.
 */
object CharacterSizing {
    const val MIN_DP = 96f
    const val MAX_DP = 220f
    private const val WIDTH_SHARE = 0.42f
    private const val HEIGHT_SHARE = 0.24f

    fun heightDp(
        windowWidthDp: Int,
        windowHeightDp: Int,
    ): Float = minOf(windowWidthDp * WIDTH_SHARE, windowHeightDp * HEIGHT_SHARE).coerceIn(MIN_DP, MAX_DP)
}
