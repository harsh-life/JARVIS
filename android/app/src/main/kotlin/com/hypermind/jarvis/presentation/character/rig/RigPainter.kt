package com.hypermind.jarvis.presentation.character.rig

import android.graphics.Bitmap
import android.graphics.BitmapShader
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Shader

/**
 * Draws a rig's triangles into a [Canvas]: the rig's setup-pose frame (plus [PADDING] for motion)
 * fitted into the view and centred, Y flipped (the runtime's Y points up), each vertex textured
 * from the atlas page by its page-pixel coordinates. One transform per frame, one draw call, no
 * allocation after construction.
 */
class RigPainter(
    page: Bitmap,
    private val frame: RigInfo,
) {
    private val paint =
        Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG).apply {
            shader = BitmapShader(page, Shader.TileMode.CLAMP, Shader.TileMode.CLAMP)
        }
    private val frameWidth = (frame.maxX - frame.minX) * (1f + 2f * PADDING)
    private val frameHeight = (frame.maxY - frame.minY) * (1f + 2f * PADDING)
    private val centreX = (frame.minX + frame.maxX) / 2f
    private val centreY = (frame.minY + frame.maxY) / 2f

    fun draw(
        canvas: Canvas,
        width: Float,
        height: Float,
        geometry: RigGeometry,
    ) {
        val empty = geometry.indexCount == 0 || frameWidth <= 0f || frameHeight <= 0f
        if (empty || width <= 0f || height <= 0f) return
        val scale = minOf(width / frameWidth, height / frameHeight)
        val save = canvas.save()
        canvas.translate(width / 2f, height / 2f)
        canvas.scale(scale, -scale)
        canvas.translate(-centreX, -centreY)
        canvas.drawVertices(
            Canvas.VertexMode.TRIANGLES,
            geometry.vertexCount * 2,
            geometry.positions,
            0,
            geometry.texs,
            0,
            null,
            0,
            geometry.indices,
            0,
            geometry.indexCount,
            paint,
        )
        canvas.restoreToCount(save)
    }

    companion object {
        /** Room around the setup frame for the hops, sways and the entrance's overshoot (per side). */
        const val PADDING = 0.06f
    }
}
