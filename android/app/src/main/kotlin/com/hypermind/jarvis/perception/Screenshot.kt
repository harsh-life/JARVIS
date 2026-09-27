package com.hypermind.jarvis.perception

import android.graphics.Bitmap
import android.os.Build
import androidx.annotation.RequiresApi
import com.hypermind.jarvis.action.Primitive
import com.hypermind.jarvis.contract.AppMetadata
import com.hypermind.jarvis.contract.Bounds
import com.hypermind.jarvis.contract.FailureReason
import com.hypermind.jarvis.contract.OperationEnvelope
import com.hypermind.jarvis.contract.PrimitiveSpec
import com.hypermind.jarvis.contract.RefusalReason
import com.hypermind.jarvis.contract.ResultEnvelope
import com.hypermind.jarvis.contract.ScreenshotResult
import com.hypermind.jarvis.contract.resultObject
import java.io.ByteArrayOutputStream
import java.util.Base64

/** One capture attempt, already encoded — or why there is none. */
sealed interface CaptureOutcome {
    class Frame(
        val webp: ByteArray,
        val width: Int,
        val height: Int,
    ) : CaptureOutcome

    data object SecureWindow : CaptureOutcome

    data object Unavailable : CaptureOutcome
}

fun interface ScreenCapture {
    suspend fun capture(maxBytes: Int): CaptureOutcome
}

/**
 * `accessibility.screenshot` — docs/23 §6 level 4, its own operation with its
 * own grid toggle (off by default), reached only after the guard has checked
 * the app is classified non-sensitive in the cached policy.
 *
 * * **One attempt.** A FLAG_SECURE window is refused as `secure_window`
 *   (Android blocks the capture; the client reports it and does not retry or
 *   try another way). Any other failure is an explicit failure.
 * * **The named app, before and after.** The app in front must be the
 *   operation's before the capture and still be after it; otherwise the frame
 *   is discarded and the operation refused `package_mismatch`.
 * * **Transient.** The frame is encoded, sent once and its buffer cleared; it
 *   is never written to storage or kept for a later attempt.
 */
class ScreenshotPrimitive(
    private val screen: ScreenSource,
    private val capture: ScreenCapture,
) : Primitive {
    override suspend fun run(
        envelope: OperationEnvelope,
        spec: PrimitiveSpec,
    ): ResultEnvelope {
        val before = screen.foreground() ?: return ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
        if (before.packageName != envelope.packageName) {
            return ResultEnvelope.refused(envelope.opId, RefusalReason.PACKAGE_MISMATCH)
        }
        // Base64 grows 4/3; keep headroom for the envelope around it.
        val budget = (spec.maxResultBytes - ENVELOPE_OVERHEAD) / 4 * 3
        return when (val outcome = capture.capture(budget)) {
            is CaptureOutcome.SecureWindow -> ResultEnvelope.refused(envelope.opId, RefusalReason.SECURE_WINDOW)
            is CaptureOutcome.Unavailable -> ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
            is CaptureOutcome.Frame ->
                try {
                    if (screen.foreground()?.packageName != envelope.packageName) {
                        ResultEnvelope.refused(envelope.opId, RefusalReason.PACKAGE_MISMATCH)
                    } else if (outcome.webp.isEmpty() || outcome.webp.size > budget) {
                        ResultEnvelope.failed(envelope.opId, FailureReason.ACTION_FAILED)
                    } else {
                        ResultEnvelope.ok(envelope.opId, resultObject(result(before, outcome)))
                    }
                } finally {
                    outcome.webp.fill(0)
                }
        }
    }

    private fun result(
        window: ForegroundWindow,
        frame: CaptureOutcome.Frame,
    ) = ScreenshotResult(
        app =
            AppMetadata(
                packageName = window.packageName,
                activity = TreeExtractor.clip(window.activity, MAX_ACTIVITY),
                windowTitle = TreeExtractor.clip(window.windowTitle, Bounds.MAX_NODE_TEXT),
            ),
        imageWebpBase64 = Base64.getEncoder().encodeToString(frame.webp),
        width = frame.width,
        height = frame.height,
    )

    private companion object {
        const val ENVELOPE_OVERHEAD = 4096
        const val MAX_ACTIVITY = 255
    }
}

/**
 * The platform capture behind [ScreenshotPrimitive]: one Accessibility
 * screenshot, downscaled and WebP-encoded within the byte budget, the bitmap
 * recycled before this returns.
 */
class AccessibilityScreenCapture(
    private val service: () -> JarvisAccessibilityService?,
) : ScreenCapture {
    override suspend fun capture(maxBytes: Int): CaptureOutcome {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return CaptureOutcome.Unavailable
        return when (val taken = service()?.capture() ?: return CaptureOutcome.Unavailable) {
            is JarvisAccessibilityService.Capture.SecureWindow -> CaptureOutcome.SecureWindow
            is JarvisAccessibilityService.Capture.Unavailable -> CaptureOutcome.Unavailable
            is JarvisAccessibilityService.Capture.Frame ->
                try {
                    encode(taken.bitmap, maxBytes)
                } finally {
                    taken.bitmap.recycle()
                }
        }
    }

    @RequiresApi(Build.VERSION_CODES.R)
    private fun encode(
        source: Bitmap,
        maxBytes: Int,
    ): CaptureOutcome {
        // Re-encodes of the one frame, smaller each time — never a new capture.
        for ((edge, quality) in ATTEMPTS) {
            val scaled = scale(source, edge)
            try {
                val bytes =
                    ByteArrayOutputStream().use { out ->
                        scaled.compress(Bitmap.CompressFormat.WEBP_LOSSY, quality, out)
                        out.toByteArray()
                    }
                if (bytes.size <= maxBytes) return CaptureOutcome.Frame(bytes, scaled.width, scaled.height)
                bytes.fill(0)
            } finally {
                if (scaled !== source) scaled.recycle()
            }
        }
        return CaptureOutcome.Unavailable
    }

    private fun scale(
        bitmap: Bitmap,
        maxEdge: Int,
    ): Bitmap {
        val longest = maxOf(bitmap.width, bitmap.height)
        if (longest <= maxEdge) return bitmap
        val factor = maxEdge.toFloat() / longest
        return Bitmap.createScaledBitmap(
            bitmap,
            (bitmap.width * factor).toInt().coerceAtLeast(1),
            (bitmap.height * factor).toInt().coerceAtLeast(1),
            true,
        )
    }

    private companion object {
        val ATTEMPTS = listOf(1280 to 60, 1024 to 45, 720 to 35)
    }
}
