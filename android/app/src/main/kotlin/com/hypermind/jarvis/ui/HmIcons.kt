package com.hypermind.jarvis.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathBuilder
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp

/**
 * The eight icons this app actually uses, drawn here.
 *
 * They used to come from `androidx.compose.material:material-icons-extended`,
 * which bundles roughly five thousand of them. Unminified, that alone pushed
 * `classes.dex` to 32MB and the APK past 10MB — for eight glyphs.
 *
 * On a product aimed at $100-150 phones with 4GB of RAM, shipping 5,000 unused
 * vector assets is the wrong trade regardless of anything else, and it removed
 * a dependency big enough to be a plausible cause of install failures on
 * low-storage devices.
 *
 * Path data is the standard Material Design icon geometry (Apache 2.0, the same
 * licence as the library it replaces).
 */
object HmIcons {
    val Bolt: ImageVector by lazy {
        icon("Bolt") {
            moveTo(11f, 21f)
            horizontalLineToRelative(-1f)
            lineToRelative(1f, -7f)
            horizontalLineTo(7.5f)
            curveToRelative(-0.58f, 0f, -0.57f, -0.32f, -0.38f, -0.66f)
            curveToRelative(0.19f, -0.34f, 0.05f, -0.08f, 0.07f, -0.12f)
            curveTo(8.48f, 10.94f, 10.42f, 7.54f, 13f, 3f)
            horizontalLineToRelative(1f)
            lineToRelative(-1f, 7f)
            horizontalLineToRelative(3.5f)
            curveToRelative(0.49f, 0f, 0.56f, 0.33f, 0.47f, 0.51f)
            lineToRelative(-0.07f, 0.15f)
            curveTo(12.96f, 17.55f, 11f, 21f, 11f, 21f)
            close()
        }
    }

    val Shield: ImageVector by lazy {
        icon("Shield") {
            moveTo(12f, 1f)
            lineTo(3f, 5f)
            verticalLineToRelative(6f)
            curveToRelative(0f, 5.55f, 3.84f, 10.74f, 9f, 12f)
            curveToRelative(5.16f, -1.26f, 9f, -6.45f, 9f, -12f)
            verticalLineTo(5f)
            lineTo(12f, 1f)
            close()
        }
    }

    /** A "tune" glyph — sliders read as settings without a 40-command gear path. */
    val Settings: ImageVector by lazy {
        icon("Settings") {
            moveTo(3f, 17f)
            verticalLineToRelative(2f)
            horizontalLineToRelative(6f)
            verticalLineToRelative(-2f)
            horizontalLineTo(3f)
            close()
            moveTo(3f, 5f)
            verticalLineToRelative(2f)
            horizontalLineToRelative(10f)
            verticalLineTo(5f)
            horizontalLineTo(3f)
            close()
            moveTo(13f, 21f)
            verticalLineToRelative(-2f)
            horizontalLineToRelative(8f)
            verticalLineToRelative(-2f)
            horizontalLineToRelative(-8f)
            verticalLineToRelative(-2f)
            horizontalLineToRelative(-2f)
            verticalLineToRelative(6f)
            horizontalLineToRelative(2f)
            close()
            moveTo(7f, 9f)
            verticalLineToRelative(2f)
            horizontalLineTo(3f)
            verticalLineToRelative(2f)
            horizontalLineToRelative(4f)
            verticalLineToRelative(2f)
            horizontalLineToRelative(2f)
            verticalLineTo(9f)
            horizontalLineTo(7f)
            close()
            moveTo(21f, 13f)
            verticalLineToRelative(-2f)
            horizontalLineTo(11f)
            verticalLineToRelative(2f)
            horizontalLineToRelative(10f)
            close()
            moveTo(15f, 9f)
            horizontalLineToRelative(2f)
            verticalLineTo(7f)
            horizontalLineToRelative(4f)
            verticalLineTo(5f)
            horizontalLineToRelative(-4f)
            verticalLineTo(3f)
            horizontalLineToRelative(-2f)
            verticalLineToRelative(6f)
            close()
        }
    }

    val Cloud: ImageVector by lazy {
        icon("Cloud") {
            moveTo(19.35f, 10.04f)
            curveTo(18.67f, 6.59f, 15.64f, 4f, 12f, 4f)
            curveTo(9.11f, 4f, 6.6f, 5.64f, 5.35f, 8.04f)
            curveTo(2.34f, 8.36f, 0f, 10.91f, 0f, 14f)
            curveToRelative(0f, 3.31f, 2.69f, 6f, 6f, 6f)
            horizontalLineToRelative(13f)
            curveToRelative(2.76f, 0f, 5f, -2.24f, 5f, -5f)
            curveToRelative(0f, -2.64f, -2.05f, -4.78f, -4.65f, -4.96f)
            close()
        }
    }

    val ArrowUpward: ImageVector by lazy {
        icon("ArrowUpward") {
            moveTo(4f, 12f)
            lineToRelative(1.41f, 1.41f)
            lineTo(11f, 7.83f)
            verticalLineTo(20f)
            horizontalLineToRelative(2f)
            verticalLineTo(7.83f)
            lineToRelative(5.58f, 5.59f)
            lineTo(20f, 12f)
            lineToRelative(-8f, -8f)
            lineToRelative(-8f, 8f)
            close()
        }
    }

    val ExpandMore: ImageVector by lazy {
        icon("ExpandMore") {
            moveTo(16.59f, 8.59f)
            lineTo(12f, 13.17f)
            lineTo(7.41f, 8.59f)
            lineTo(6f, 10f)
            lineToRelative(6f, 6f)
            lineToRelative(6f, -6f)
            close()
        }
    }

    val Visibility: ImageVector by lazy {
        icon("Visibility") {
            moveTo(12f, 4.5f)
            curveTo(7f, 4.5f, 2.73f, 7.61f, 1f, 12f)
            curveToRelative(1.73f, 4.39f, 6f, 7.5f, 11f, 7.5f)
            reflectiveCurveToRelative(9.27f, -3.11f, 11f, -7.5f)
            curveToRelative(-1.73f, -4.39f, -6f, -7.5f, -11f, -7.5f)
            close()
            moveTo(12f, 17f)
            curveToRelative(-2.76f, 0f, -5f, -2.24f, -5f, -5f)
            reflectiveCurveToRelative(2.24f, -5f, 5f, -5f)
            reflectiveCurveToRelative(5f, 2.24f, 5f, 5f)
            reflectiveCurveToRelative(-2.24f, 5f, -5f, 5f)
            close()
            moveTo(12f, 9f)
            curveToRelative(-1.66f, 0f, -3f, 1.34f, -3f, 3f)
            reflectiveCurveToRelative(1.34f, 3f, 3f, 3f)
            reflectiveCurveToRelative(3f, -1.34f, 3f, -3f)
            reflectiveCurveToRelative(-1.34f, -3f, -3f, -3f)
            close()
        }
    }

    val VisibilityOff: ImageVector by lazy {
        icon("VisibilityOff") {
            moveTo(12f, 7f)
            curveToRelative(2.76f, 0f, 5f, 2.24f, 5f, 5f)
            curveToRelative(0f, 0.65f, -0.13f, 1.26f, -0.36f, 1.83f)
            lineToRelative(2.92f, 2.92f)
            curveToRelative(1.51f, -1.26f, 2.7f, -2.89f, 3.43f, -4.75f)
            curveToRelative(-1.73f, -4.39f, -6f, -7.5f, -11f, -7.5f)
            curveToRelative(-1.4f, 0f, -2.74f, 0.25f, -3.98f, 0.7f)
            lineToRelative(2.16f, 2.16f)
            curveTo(10.74f, 7.13f, 11.35f, 7f, 12f, 7f)
            close()
            moveTo(2f, 4.27f)
            lineToRelative(2.28f, 2.28f)
            lineToRelative(0.46f, 0.46f)
            curveTo(3.08f, 8.3f, 1.78f, 10.02f, 1f, 12f)
            curveToRelative(1.73f, 4.39f, 6f, 7.5f, 11f, 7.5f)
            curveToRelative(1.55f, 0f, 3.03f, -0.3f, 4.38f, -0.84f)
            lineToRelative(0.42f, 0.42f)
            lineTo(19.73f, 22f)
            lineTo(21f, 20.73f)
            lineTo(3.27f, 3f)
            lineTo(2f, 4.27f)
            close()
        }
    }

    /** Memory-chip glyph — the wordmark lockup in DESIGN.HTML. */
    val Chip: ImageVector by lazy {
        icon("Chip") {
            // body
            moveTo(7f, 7f)
            horizontalLineTo(17f)
            verticalLineTo(17f)
            horizontalLineTo(7f)
            close()
            // pins, three per side
            moveTo(9f, 3f)
            horizontalLineToRelative(1.5f)
            verticalLineTo(6f)
            horizontalLineTo(9f)
            close()
            moveTo(13.5f, 3f)
            horizontalLineToRelative(1.5f)
            verticalLineTo(6f)
            horizontalLineToRelative(-1.5f)
            close()
            moveTo(9f, 18f)
            horizontalLineToRelative(1.5f)
            verticalLineTo(21f)
            horizontalLineTo(9f)
            close()
            moveTo(13.5f, 18f)
            horizontalLineToRelative(1.5f)
            verticalLineTo(21f)
            horizontalLineToRelative(-1.5f)
            close()
            moveTo(3f, 9f)
            horizontalLineTo(6f)
            verticalLineToRelative(1.5f)
            horizontalLineTo(3f)
            close()
            moveTo(3f, 13.5f)
            horizontalLineTo(6f)
            verticalLineToRelative(1.5f)
            horizontalLineTo(3f)
            close()
            moveTo(18f, 9f)
            horizontalLineTo(21f)
            verticalLineToRelative(1.5f)
            horizontalLineToRelative(-3f)
            close()
            moveTo(18f, 13.5f)
            horizontalLineTo(21f)
            verticalLineToRelative(1.5f)
            horizontalLineToRelative(-3f)
            close()
        }
    }

    private fun icon(name: String, pathData: PathBuilder.() -> Unit): ImageVector =
        ImageVector
            .Builder(
                name = name,
                defaultWidth = 24.dp,
                defaultHeight = 24.dp,
                viewportWidth = 24f,
                viewportHeight = 24f,
            ).apply {
                // White fill; every call site tints it via LocalContentColor.
                path(fill = SolidColor(Color.White), pathBuilder = pathData)
            }.build()
}
