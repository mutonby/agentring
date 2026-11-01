package dev.agentring.app.ui

import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.painter.Painter
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import dev.agentring.app.R
import dev.agentring.app.call.CallPermissions
import dev.agentring.app.call.CallPermissions.Item

private fun Item.label() = when (this) {
    Item.NOTIFICATIONS -> R.string.perm_notifications
    Item.MICROPHONE -> R.string.perm_microphone
    Item.FULL_SCREEN -> R.string.perm_full_screen
    Item.BATTERY -> R.string.perm_battery
    Item.BACKGROUND -> R.string.perm_background
    Item.DND -> R.string.perm_dnd
}

private fun Item.why() = when (this) {
    Item.NOTIFICATIONS -> R.string.perm_notifications_why
    Item.MICROPHONE -> R.string.perm_microphone_why
    Item.FULL_SCREEN -> R.string.perm_full_screen_why
    Item.BATTERY -> R.string.perm_battery_why
    Item.BACKGROUND -> R.string.perm_background_why
    Item.DND -> R.string.perm_dnd_why
}

@Composable
private fun Item.icon(): Painter = when (this) {
    Item.NOTIFICATIONS -> rememberVectorPainter(Icons.Filled.Notifications)
    Item.MICROPHONE -> painterResource(R.drawable.ic_mic)
    Item.FULL_SCREEN -> rememberVectorPainter(Icons.Filled.Lock)
    Item.BATTERY -> painterResource(R.drawable.ic_battery)
    Item.BACKGROUND -> rememberVectorPainter(Icons.Filled.Refresh)
    Item.DND -> painterResource(R.drawable.ic_bolt)
}

/** True when every required item is in place; re-checked every time the screen resumes. */
@Composable
fun rememberCallsReady(): Boolean {
    val ctx = LocalContext.current
    var tick by remember { mutableIntStateOf(0) }
    LifecycleResumeEffect(Unit) {
        tick++
        onPauseOrDispose {}
    }
    return remember(tick) { CallPermissions.required.all { CallPermissions.isOk(ctx, it) } }
}

/** Checklist of what calls need, each with a button that fixes it. */
@Composable
fun CallReadinessCard(modifier: Modifier = Modifier, showHeader: Boolean = true) {
    val ctx = LocalContext.current
    var tick by remember { mutableIntStateOf(0) }
    LifecycleResumeEffect(Unit) {
        tick++
        onPauseOrDispose {}
    }
    val mic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { tick++ }
    val notif = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { tick++ }
    val status = remember(tick) { Item.entries.associateWith { CallPermissions.isOk(ctx, it) } }
    val colors = LocalStatusColors.current

    BrandCard(modifier.testTag("call_readiness")) {
        Column(Modifier.padding(vertical = 8.dp)) {
            if (showHeader) {
                Column(Modifier.padding(horizontal = 20.dp, vertical = 12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(stringResource(R.string.perm_title), style = MaterialTheme.typography.titleMedium)
                    Text(stringResource(R.string.perm_body), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            }
            Item.entries.forEachIndexed { i, item ->
                if (i > 0) HorizontalDivider(Modifier.padding(start = 72.dp), color = MaterialTheme.colorScheme.outlineVariant)
                val ok = status[item] == true
                val optional = item !in CallPermissions.required && item != Item.BATTERY
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(14.dp),
                ) {
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .size(40.dp)
                            .background(
                                if (ok) colors.ok.copy(alpha = 0.14f) else MaterialTheme.colorScheme.surfaceVariant,
                                CircleShape,
                            ),
                    ) {
                        if (ok) {
                            Icon(Icons.Filled.Check, null, tint = colors.ok, modifier = Modifier.size(22.dp))
                        } else {
                            Icon(item.icon(), null, tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(20.dp))
                        }
                    }
                    Column(Modifier.weight(1f)) {
                        Text(stringResource(item.label()), style = MaterialTheme.typography.bodyLarge)
                        Text(
                            if (optional && !ok) stringResource(R.string.perm_optional_why, stringResource(item.why())) else stringResource(item.why()),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    if (!ok) {
                        FilledTonalButton(
                            onClick = {
                                when (item) {
                                    Item.MICROPHONE -> mic.launch(Manifest.permission.RECORD_AUDIO)
                                    Item.NOTIFICATIONS -> if (android.os.Build.VERSION.SDK_INT >= 33) {
                                        notif.launch(Manifest.permission.POST_NOTIFICATIONS)
                                    } else {
                                        ctx.startActivity(CallPermissions.settingsIntent(ctx, item))
                                    }
                                    else -> runCatching { ctx.startActivity(CallPermissions.settingsIntent(ctx, item)) }
                                }
                            },
                            contentPadding = PaddingValues(horizontal = 14.dp),
                            modifier = Modifier.height(36.dp).testTag("fix_${item.name.lowercase()}"),
                        ) { Text(stringResource(R.string.perm_fix)) }
                    }
                }
            }
            Text(
                stringResource(R.string.perm_oem_hint),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 12.dp),
            )
        }
    }
}
