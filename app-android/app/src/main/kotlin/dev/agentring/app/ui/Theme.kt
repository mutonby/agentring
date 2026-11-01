package dev.agentring.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// Same palette as the agentring.io website (warm neutrals + green accent), so the app and the
// site read as one product. Dynamic color is off on purpose: the brand stays recognisable.
private val Light = lightColorScheme(
    primary = Color(0xFF0B7A4B),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDFF1E7),
    onPrimaryContainer = Color(0xFF08663F),
    secondary = Color(0xFF4A4C55),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFEFEDE6),
    onSecondaryContainer = Color(0xFF15161A),
    tertiary = Color(0xFF1D5FD6),
    background = Color(0xFFF7F6F2),
    onBackground = Color(0xFF15161A),
    surface = Color(0xFFF7F6F2),
    onSurface = Color(0xFF15161A),
    surfaceVariant = Color(0xFFEFEDE6),
    onSurfaceVariant = Color(0xFF4A4C55),
    surfaceContainerLowest = Color.White,
    surfaceContainerLow = Color(0xFFFFFFFF),
    surfaceContainer = Color(0xFFFFFFFF),
    surfaceContainerHigh = Color(0xFFF2F0EA),
    surfaceContainerHighest = Color(0xFFEFEDE6),
    outline = Color(0xFFC4C0B3),
    outlineVariant = Color(0xFFDCD9CF),
    error = Color(0xFFB42318),
    onError = Color.White,
    errorContainer = Color(0xFFFBE4E1),
    onErrorContainer = Color(0xFF7A1A12),
)

private val Dark = darkColorScheme(
    primary = Color(0xFF43D68F),
    onPrimary = Color(0xFF06170E),
    primaryContainer = Color(0xFF10291D),
    onPrimaryContainer = Color(0xFF5FE3A2),
    secondary = Color(0xFFB4B6BD),
    onSecondary = Color(0xFF15161A),
    secondaryContainer = Color(0xFF22262C),
    onSecondaryContainer = Color(0xFFECEBE6),
    tertiary = Color(0xFF7FB0FF),
    background = Color(0xFF0E1013),
    onBackground = Color(0xFFECEBE6),
    surface = Color(0xFF0E1013),
    onSurface = Color(0xFFECEBE6),
    surfaceVariant = Color(0xFF1C2026),
    onSurfaceVariant = Color(0xFFB4B6BD),
    surfaceContainerLowest = Color(0xFF0B0D10),
    surfaceContainerLow = Color(0xFF13161A),
    surfaceContainer = Color(0xFF171A1F),
    surfaceContainerHigh = Color(0xFF1C2026),
    surfaceContainerHighest = Color(0xFF22262C),
    outline = Color(0xFF363B44),
    outlineVariant = Color(0xFF262A31),
    error = Color(0xFFFF8A80),
    onError = Color(0xFF3B0A06),
    errorContainer = Color(0xFF3A1512),
    onErrorContainer = Color(0xFFFFDAD5),
)

/** Status colours that Material's scheme has no slot for. */
@Immutable
data class StatusColors(val ok: Color, val warn: Color, val bad: Color, val idle: Color)

private val LightStatus = StatusColors(ok = Color(0xFF0B7A4B), warn = Color(0xFFB7791F), bad = Color(0xFFB42318), idle = Color(0xFF8A8C94))
private val DarkStatus = StatusColors(ok = Color(0xFF43D68F), warn = Color(0xFFF0B44C), bad = Color(0xFFFF8A80), idle = Color(0xFF6A6C75))

val LocalStatusColors = staticCompositionLocalOf { LightStatus }

private val Type = Typography().let { t ->
    t.copy(
        displaySmall = t.displaySmall.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.5).sp),
        headlineLarge = t.headlineLarge.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.5).sp),
        headlineMedium = t.headlineMedium.copy(fontWeight = FontWeight.SemiBold, letterSpacing = (-0.25).sp),
        headlineSmall = t.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
        titleLarge = t.titleLarge.copy(fontWeight = FontWeight.SemiBold),
        titleMedium = t.titleMedium.copy(fontWeight = FontWeight.SemiBold),
        labelLarge = t.labelLarge.copy(fontWeight = FontWeight.SemiBold, letterSpacing = 0.sp),
        bodyLarge = t.bodyLarge.copy(lineHeight = 24.sp),
    )
}

private val AppShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(32.dp),
)

val MonoStyle = TextStyle(fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace, fontSize = 13.sp, lineHeight = 20.sp)

@Composable
fun AgentringTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    CompositionLocalProvider(LocalStatusColors provides if (dark) DarkStatus else LightStatus) {
        MaterialTheme(colorScheme = if (dark) Dark else Light, typography = Type, shapes = AppShapes, content = content)
    }
}
