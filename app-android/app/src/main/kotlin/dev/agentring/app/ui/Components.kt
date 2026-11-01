package dev.agentring.app.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.painter.Painter
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import dev.agentring.app.R

/** Round badge with a soft green glow: the visual anchor of every onboarding page. */
@Composable
fun HeroBadge(icon: Painter, size: Dp = 112.dp, pulse: Boolean = false) {
    val scheme = MaterialTheme.colorScheme
    val ring = if (pulse) {
        val t = rememberInfiniteTransition(label = "pulse")
        t.animateFloat(1f, 1.12f, infiniteRepeatable(tween(1100), RepeatMode.Reverse), label = "ring").value
    } else {
        1f
    }
    Box(contentAlignment = Alignment.Center, modifier = Modifier.size(size * 1.35f)) {
        Box(
            Modifier
                .size(size * 1.3f)
                .scale(ring)
                .background(
                    Brush.radialGradient(listOf(scheme.primary.copy(alpha = 0.22f), Color.Transparent)),
                    CircleShape,
                ),
        )
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(size)
                .background(
                    Brush.linearGradient(listOf(scheme.primary, scheme.primary.copy(alpha = 0.78f))),
                    CircleShape,
                ),
        ) {
            Icon(icon, null, tint = scheme.onPrimary, modifier = Modifier.size(size * 0.46f))
        }
    }
}

/** "1  Do this" row used by the setup tutorial. */
@Composable
fun NumberedStep(n: Int, title: String, body: String? = null, extra: (@Composable () -> Unit)? = null) {
    Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier.size(28.dp).background(MaterialTheme.colorScheme.primaryContainer, CircleShape),
        ) {
            Text(
                "$n",
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
        }
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
            if (body != null) {
                Text(body, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            extra?.invoke()
        }
    }
}

/** A command the user types on a computer, with a copy button. */
@Composable
fun CommandBlock(command: String, modifier: Modifier = Modifier) {
    val clipboard = LocalClipboardManager.current
    Surface(
        color = Color(0xFF15161A),
        contentColor = Color(0xFFE9E7E1),
        shape = RoundedCornerShape(12.dp),
        modifier = modifier.fillMaxWidth(),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 14.dp)) {
            Text(command, style = MonoStyle, modifier = Modifier.weight(1f).padding(vertical = 12.dp))
            IconButton(onClick = { clipboard.setText(AnnotatedString(command)) }) {
                Icon(painterResource(R.drawable.ic_copy), stringResource(R.string.copy), tint = Color(0xFF9A9CA4), modifier = Modifier.size(18.dp))
            }
        }
    }
}

/** Small coloured dot + label. */
@Composable
fun StatusPill(text: String, color: Color, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .clip(CircleShape)
            .background(color.copy(alpha = 0.12f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
    ) {
        Box(Modifier.size(8.dp).background(color, CircleShape))
        Spacer(Modifier.width(6.dp))
        Text(text, style = MaterialTheme.typography.labelMedium, color = color)
    }
}

/** White (dark: raised) card with a hairline border, like the cards on the website. */
@Composable
fun BrandCard(
    modifier: Modifier = Modifier,
    selected: Boolean = false,
    onClick: (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    val scheme = MaterialTheme.colorScheme
    val border = BorderStroke(if (selected) 2.dp else 1.dp, if (selected) scheme.primary else scheme.outlineVariant)
    if (onClick != null) {
        Surface(
            onClick = onClick,
            shape = MaterialTheme.shapes.large,
            color = scheme.surfaceContainer,
            border = border,
            modifier = modifier.fillMaxWidth(),
        ) { content() }
    } else {
        Surface(
            shape = MaterialTheme.shapes.large,
            color = scheme.surfaceContainer,
            border = border,
            modifier = modifier.fillMaxWidth(),
        ) { content() }
    }
}

/** Avatar circle with the agent's initial. */
@Composable
fun AgentAvatar(name: String, size: Dp = 56.dp, online: Boolean? = null) {
    val scheme = MaterialTheme.colorScheme
    val status = LocalStatusColors.current
    Box {
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(size)
                .background(Brush.linearGradient(listOf(scheme.primary, scheme.primary.copy(alpha = 0.7f))), CircleShape),
        ) {
            Text(
                name.trim().take(1).uppercase().ifEmpty { "A" },
                style = MaterialTheme.typography.headlineSmall,
                color = scheme.onPrimary,
            )
        }
        if (online != null) {
            Box(
                Modifier
                    .align(Alignment.BottomEnd)
                    .size(size * 0.28f)
                    .background(if (online) status.ok else status.idle, CircleShape)
                    .border(3.dp, scheme.background, CircleShape),
            )
        }
    }
}
