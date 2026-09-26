package com.hypermind.jarvis.perception

import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * docs/23 §6 level 3 with ML Kit's **bundled** Latin model (adapted from the
 * donor's `TextRecognizer`): recognition runs on the device, with no model
 * download and no image upload. The frame is captured, read and recycled
 * inside [readScreen]; it is never stored, logged or sent — only the text
 * blocks leave this function.
 */
class MlKitScreenOcr(
    private val service: () -> JarvisAccessibilityService?,
) : ScreenOcr {
    private val client by lazy { TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS) }

    override suspend fun readScreen(): List<OcrText>? {
        val bitmap = service()?.capture() ?: return null
        return try {
            suspendCancellableCoroutine { cont ->
                client
                    .process(InputImage.fromBitmap(bitmap, 0))
                    .addOnSuccessListener { text ->
                        val blocks =
                            text.textBlocks.mapNotNull { block ->
                                val box = block.boundingBox ?: return@mapNotNull null
                                OcrText(block.text, listOf(box.left, box.top, box.right, box.bottom))
                            }
                        if (cont.isActive) cont.resume(blocks)
                    }.addOnFailureListener { if (cont.isActive) cont.resume(null) }
            }
        } finally {
            bitmap.recycle()
        }
    }
}
