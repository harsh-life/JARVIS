package com.hypermind.jarvis.ui.theme

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat

/*
 * Design tokens transcribed from DESIGN.HTML (the Stitch output).
 *
 * Three things in that design are deliberate departures from what this app had,
 * and all three are improvements worth keeping:
 *
 * 1. **Corners are sharp.** 4dp on cards, not 14dp. Rounded cards read as a
 *    consumer chat app; 4dp reads as an instrument panel, which is the whole
 *    positioning.
 * 2. **The outline is warm** (#4F4537, a brown-grey) rather than a cool navy.
 *    Against the cold #080C16 background it gives the brass-on-dark feel of
 *    lab equipment instead of the blue-on-blue of every dashboard template.
 * 3. **Primary is brighter** (#F3BE65 vs #D4A24C). #D4A24C survives as
 *    primaryContainer for fills, where the brighter tone would be too loud.
 */

// -- surfaces ---------------------------------------------------------------
val Background = Color(0xFF080C16)
val Surface = Color(0xFF0F131D)
val SurfaceContainerLowest = Color(0xFF0A0E18)
val SurfaceContainerLow = Color(0xFF171B26)
val SurfaceContainer = Color(0xFF1B1F2A)
val SurfaceContainerHigh = Color(0xFF262A35)
val SurfaceVariant = Color(0xFF313540)
val SurfaceBright = Color(0xFF353944)

// -- accents ----------------------------------------------------------------
val Primary = Color(0xFFF3BE65)
val PrimaryContainer = Color(0xFFD4A24C)
val OnPrimary = Color(0xFF432C00)

/** Allowed / read-only. */
val Secondary = Color(0xFF6FDBA7)
val SecondaryContainer = Color(0xFF31A374)

/** Refused. */
val ErrorRed = Color(0xFFFFB4AB)
val ErrorContainer = Color(0xFF93000A)

// -- text and lines ---------------------------------------------------------
val OnSurface = Color(0xFFDFE2F1)
val OnSurfaceVariant = Color(0xFFD3C4B2)
val Outline = Color(0xFF9C8F7E)
val OutlineVariant = Color(0xFF4F4537)

// Kept so existing call sites keep compiling; mapped onto the new palette.
val Navy400 = Color(0xFF4A6FA5)
val Gold500 = Primary
val Gold300 = Color(0xFFFFDEAC)
val Brick500 = ErrorRed
val Green500 = Secondary
val Slate300 = OnSurfaceVariant
val Slate100 = OnSurface

private val DarkScheme =
    darkColorScheme(
        primary = Primary,
        onPrimary = OnPrimary,
        primaryContainer = PrimaryContainer,
        onPrimaryContainer = Color(0xFF563A00),
        secondary = Secondary,
        onSecondary = Color(0xFF003824),
        secondaryContainer = SecondaryContainer,
        background = Background,
        onBackground = OnSurface,
        surface = Surface,
        onSurface = OnSurface,
        surfaceVariant = SurfaceVariant,
        onSurfaceVariant = OnSurfaceVariant,
        surfaceContainerLowest = SurfaceContainerLowest,
        surfaceContainerLow = SurfaceContainerLow,
        surfaceContainer = SurfaceContainer,
        surfaceContainerHigh = SurfaceContainerHigh,
        surfaceBright = SurfaceBright,
        error = ErrorRed,
        onError = Color(0xFF690005),
        errorContainer = ErrorContainer,
        outline = Outline,
        outlineVariant = OutlineVariant,
    )

/**
 * The light scheme is derived, not designed — DESIGN.HTML only specifies dark.
 * It exists so the app is legible if a user's system is set to light, and it
 * keeps the same warm-outline / brass-accent relationships rather than
 * inventing a second visual language.
 */
private val LightScheme =
    lightColorScheme(
        primary = Color(0xFF7E5700),
        onPrimary = Color.White,
        primaryContainer = Color(0xFFFFDEAC),
        onPrimaryContainer = Color(0xFF281900),
        secondary = Color(0xFF006B47),
        secondaryContainer = Color(0xFF8BF8C2),
        background = Color(0xFFF7F8FB),
        onBackground = Color(0xFF1A1C22),
        surface = Color.White,
        onSurface = Color(0xFF1A1C22),
        surfaceVariant = Color(0xFFE8ECF3),
        onSurfaceVariant = Color(0xFF4A5468),
        surfaceContainerLowest = Color.White,
        surfaceContainerLow = Color(0xFFF2F4F8),
        surfaceContainer = Color(0xFFECEFF4),
        surfaceContainerHigh = Color(0xFFE6E9F0),
        error = Color(0xFF9C2F24),
        errorContainer = Color(0xFFFFDAD6),
        outline = Color(0xFF7A7266),
        outlineVariant = Color(0xFFC9C2B6),
    )

/**
 * Sharp by design. DESIGN.HTML sets the default radius to 0.25rem (4px) and
 * reserves the pill shape for chips and the input field. Cards are square-ish
 * on purpose; see the note at the top of this file.
 */
private val AppShapes =
    Shapes(
        extraSmall = RoundedCornerShape(2.dp),
        small = RoundedCornerShape(4.dp),
        medium = RoundedCornerShape(4.dp),
        large = RoundedCornerShape(8.dp),
        extraLarge = RoundedCornerShape(12.dp),
    )

private val AppTypography =
    Typography(
        displaySmall =
            TextStyle(
                fontFamily = FontFamily.SansSerif,
                fontWeight = FontWeight.SemiBold,
                fontSize = 28.sp,
                lineHeight = 36.sp,
                letterSpacing = (-0.02).em,
            ),
        titleLarge =
            TextStyle(
                fontFamily = FontFamily.SansSerif,
                fontWeight = FontWeight.SemiBold,
                fontSize = 20.sp,
                lineHeight = 28.sp,
                letterSpacing = (-0.01).em,
            ),
        bodyMedium =
            TextStyle(
                fontFamily = FontFamily.SansSerif,
                fontWeight = FontWeight.Normal,
                fontSize = 15.sp,
                lineHeight = 22.sp,
            ),
        // Monospace on purpose: these are technical status labels sitting next to
        // command text, and the fixed width keeps columns aligned.
        labelSmall =
            TextStyle(
                fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.Medium,
                fontSize = 11.sp,
                lineHeight = 14.sp,
                letterSpacing = 0.04.em,
            ),
        labelMedium =
            TextStyle(
                fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.Medium,
                fontSize = 13.sp,
                lineHeight = 17.sp,
                letterSpacing = 0.02.em,
            ),
        // Sans, not monospace. bodySmall carries explanatory prose — empty-state
        // copy, gate reasons, review notes. Commands set FontFamily.Monospace
        // explicitly at their call sites, which is where it belongs.
        bodySmall =
            TextStyle(
                fontFamily = FontFamily.SansSerif,
                fontSize = 13.sp,
                lineHeight = 18.sp,
            ),
    )

/**
 * Dark by default, regardless of the system setting.
 *
 * DESIGN.HTML is `<html class="dark">` throughout — the palette, the warm
 * outline and the brass accent were all chosen against #080C16. Rendered on a
 * light background the same design reads washed out and generic, which is the
 * opposite of the point. This is an instrument, and instruments are dark.
 *
 * The light scheme is kept as a fallback for anyone who forces it, but nothing
 * opts into it automatically.
 */
@Composable
fun JarvisTheme(
    darkTheme: Boolean = true,
    content: @Composable () -> Unit,
) {
    val scheme = if (darkTheme) DarkScheme else LightScheme
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            // The floating overlay hosts this same theme from inside a Service,
            // where there is no Activity and no window to tint. `view.context as
            // Activity` crashed the whole app the moment the orb was switched on.
            // Unwrap rather than cast, and skip the tint when there is no
            // Activity — a window that does not exist needs no status bar colour.
            // The app draws edge to edge (MainActivity), so the bars take the
            // background; only their icon appearance needs setting.
            view.context.findActivity()?.let { activity ->
                WindowCompat
                    .getInsetsController(activity.window, view)
                    .isAppearanceLightStatusBars = !darkTheme
            }
        }
    }
    MaterialTheme(
        colorScheme = scheme,
        typography = AppTypography,
        shapes = AppShapes,
        content = content,
    )
}

/**
 * Compose's LocalView context is often a ContextWrapper (themed wrappers,
 * service contexts), so an `as Activity` cast is wrong even inside an Activity.
 * Walk the wrapper chain instead.
 */
private tailrec fun Context.findActivity(): Activity? =
    when (this) {
        is Activity -> this
        is ContextWrapper -> baseContext.findActivity()
        else -> null
    }
