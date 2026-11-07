package dev.agentring.app.ui

import android.content.Intent
import android.net.Uri
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import kotlinx.coroutines.delay
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.painter.Painter
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import dev.agentring.app.BuildConfig
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.app.data.Pairing
import dev.agentring.app.notify.Notifications
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private enum class Step { WELCOME, HOW, PAIR, CALLS, DONE }

/** Where the user's relay lives; only changes which instructions the tutorial shows. */
private enum class Setup { CLOUD, SELF_HOSTED }

/** True when the pairing points at the hosted service this build links to. */
internal fun Pairing.isCloud(): Boolean {
    val cloud = Uri.parse(BuildConfig.CLOUD_URL.ifEmpty { return false }).host ?: return false
    return Uri.parse(relayUrl).host.equals(cloud, ignoreCase = true)
}

/** The hosted service's dashboard (usage, plan), or null when the relay is self-hosted. */
internal fun Pairing.dashboardUrl(): String? = if (isCloud()) BuildConfig.CLOUD_URL.trimEnd('/') + "/app#usage" else null

@Composable
fun OnboardingFlow(paired: Pairing?, onFinished: () -> Unit) {
    var step by rememberSaveable { mutableStateOf(if (paired != null) Step.CALLS else Step.WELCOME) }
    var setup by rememberSaveable {
        mutableStateOf(if (paired?.isCloud() == true || (paired == null && BuildConfig.CLOUD_URL.isNotEmpty())) Setup.CLOUD else Setup.SELF_HOSTED)
    }
    // Once paired there is no going back to the pairing pages.
    val canGoBack = step == Step.HOW || step == Step.PAIR
    // Builds without a hosted service skip the "cloud or own server" question.
    val offerCloud = BuildConfig.CLOUD_URL.isNotEmpty()
    fun back() {
        step = if (step == Step.PAIR && !offerCloud) Step.WELCOME else Step.entries[step.ordinal - 1]
    }
    BackHandler(enabled = canGoBack) { back() }

    Scaffold(
        topBar = {
            if (step != Step.WELCOME) {
                TutorialTopBar(
                    index = step.ordinal,
                    total = Step.entries.size - 1,
                    onBack = if (canGoBack) ::back else null,
                )
            }
        },
    ) { padding ->
        AnimatedContent(
            targetState = step,
            transitionSpec = {
                val dir = if (targetState.ordinal > initialState.ordinal) 1 else -1
                (slideInHorizontally { it * dir / 4 } + fadeIn()) togetherWith (slideOutHorizontally { -it * dir / 4 } + fadeOut())
            },
            modifier = Modifier.padding(padding).fillMaxSize(),
            label = "onboarding",
        ) { s ->
            when (s) {
                Step.WELCOME -> WelcomePage(onNext = { step = if (offerCloud) Step.HOW else Step.PAIR })
                Step.HOW -> HowPage(setup) {
                    setup = it
                    step = Step.PAIR
                }
                Step.PAIR -> PairPage(setup, onPaired = { step = Step.CALLS })
                Step.CALLS -> CallsPage(onNext = { step = Step.DONE })
                Step.DONE -> DonePage(Graph.pairing.current ?: paired, onFinished)
            }
        }
    }
}

@Composable
private fun TutorialTopBar(index: Int, total: Int, onBack: (() -> Unit)?) {
    val progress by animateFloatAsState(index / total.toFloat(), label = "progress")
    Column(Modifier.fillMaxWidth().windowInsetsPadding(WindowInsets.statusBars).padding(top = 8.dp)) {
        Row(Modifier.fillMaxWidth().height(48.dp).padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            if (onBack != null) {
                IconButton(onClick = onBack, modifier = Modifier.testTag("back")) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back))
                }
            } else {
                Spacer(Modifier.width(48.dp))
            }
            Spacer(Modifier.weight(1f))
            Text(
                stringResource(R.string.step_of, index, total),
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(end = 20.dp),
            )
        }
        LinearProgressIndicator(
            progress = { progress },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 24.dp).height(6.dp),
            trackColor = MaterialTheme.colorScheme.outlineVariant,
            drawStopIndicator = {},
        )
    }
}

/** Scrollable page body with a pinned bottom action area. */
@Composable
private fun Page(
    bottom: @Composable () -> Unit,
    content: @Composable () -> Unit,
) {
    Column(Modifier.fillMaxSize().imePadding()) {
        Column(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp, vertical = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) { content() }
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) { bottom() }
    }
}

@Composable
private fun PrimaryButton(text: String, tag: String, enabled: Boolean = true, onClick: () -> Unit) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = Modifier.fillMaxWidth().height(56.dp).testTag(tag),
    ) { Text(text, style = MaterialTheme.typography.titleMedium) }
}

// ---------------------------------------------------------------- pages

@Composable
private fun WelcomePage(onNext: () -> Unit) {
    Page(bottom = {
        PrimaryButton(stringResource(R.string.get_started), "get_started", onClick = onNext)
        Text(
            stringResource(R.string.welcome_footer),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }) {
        Spacer(Modifier.height(24.dp))
        HeroBadge(painterResource(R.drawable.ic_call), pulse = true)
        Text(
            stringResource(R.string.welcome_title),
            style = MaterialTheme.typography.headlineLarge,
            textAlign = TextAlign.Center,
            modifier = Modifier.testTag("title"),
        )
        Text(
            stringResource(R.string.welcome_body),
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(4.dp))
        Feature(painterResource(R.drawable.ic_chat), R.string.feature_messages_title, R.string.feature_messages_body)
        Feature(painterResource(R.drawable.ic_call), R.string.feature_calls_title, R.string.feature_calls_body)
        Feature(painterResource(R.drawable.ic_mic), R.string.feature_callback_title, R.string.feature_callback_body)
    }
}

@Composable
private fun Feature(icon: Painter, title: Int, body: Int) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(16.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier.size(48.dp).background(MaterialTheme.colorScheme.primaryContainer, MaterialTheme.shapes.medium),
        ) {
            Icon(icon, null, tint = MaterialTheme.colorScheme.onPrimaryContainer, modifier = Modifier.size(24.dp))
        }
        Column(Modifier.weight(1f)) {
            Text(stringResource(title), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(body), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun HowPage(current: Setup, onPick: (Setup) -> Unit) {
    Page(bottom = {}) {
        PageHeader(R.string.how_title, R.string.how_body)
        SetupOption(
            icon = painterResource(R.drawable.ic_cloud),
            title = stringResource(R.string.how_cloud_title),
            badge = stringResource(R.string.how_cloud_badge),
            body = stringResource(R.string.how_cloud_body),
            selected = current == Setup.CLOUD,
            tag = "setup_cloud",
        ) { onPick(Setup.CLOUD) }
        SetupOption(
            icon = painterResource(R.drawable.ic_server),
            title = stringResource(R.string.how_self_title),
            badge = stringResource(R.string.how_self_badge),
            body = stringResource(R.string.how_self_body),
            selected = current == Setup.SELF_HOSTED,
            tag = "setup_self",
        ) { onPick(Setup.SELF_HOSTED) }
        Text(
            stringResource(R.string.how_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}

@Composable
private fun SetupOption(icon: Painter, title: String, badge: String, body: String, selected: Boolean, tag: String, onClick: () -> Unit) {
    BrandCard(selected = selected, onClick = onClick, modifier = Modifier.testTag(tag)) {
        Row(Modifier.padding(20.dp), horizontalArrangement = Arrangement.spacedBy(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier.size(52.dp).background(MaterialTheme.colorScheme.primaryContainer, MaterialTheme.shapes.medium),
            ) {
                Icon(icon, null, tint = MaterialTheme.colorScheme.onPrimaryContainer, modifier = Modifier.size(28.dp))
            }
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(title, style = MaterialTheme.typography.titleMedium)
                    Surface(color = MaterialTheme.colorScheme.primaryContainer, shape = MaterialTheme.shapes.extraSmall) {
                        Text(
                            badge,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onPrimaryContainer,
                            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                        )
                    }
                }
                Text(body, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Icon(Icons.Filled.KeyboardArrowRight, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun PageHeader(title: Int, body: Int, icon: Painter? = null) {
    if (icon != null) HeroBadge(icon, size = 80.dp)
    Text(
        stringResource(title),
        style = MaterialTheme.typography.headlineMedium,
        textAlign = TextAlign.Center,
        modifier = Modifier.fillMaxWidth().testTag("title"),
    )
    Text(
        stringResource(body),
        style = MaterialTheme.typography.bodyLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        textAlign = TextAlign.Center,
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun PairPage(setup: Setup, onPaired: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var code by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var showPaste by rememberSaveable { mutableStateOf(false) }
    // The paste field opens below the steps, out of sight on most phones: scroll to it and focus it.
    val pasteInView = remember { BringIntoViewRequester() }
    val pasteFocus = remember { FocusRequester() }
    LaunchedEffect(showPaste) {
        if (showPaste) {
            delay(300) // let AnimatedVisibility lay the field out first
            pasteInView.bringIntoView()
            runCatching { pasteFocus.requestFocus() }
        }
    }

    fun pair(raw: String) {
        if (busy) return
        scope.launch {
            busy = true
            error = null
            try {
                withContext(Dispatchers.IO) { Graph.pairWith(raw) }
                onPaired()
            } catch (e: Exception) {
                error = e.message ?: e.javaClass.simpleName
            } finally {
                busy = false
            }
        }
    }
    val scanFailed = stringResource(R.string.scan_failed, "%s")
    fun scan() {
        val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        GmsBarcodeScanning.getClient(ctx, options).startScan()
            .addOnSuccessListener { barcode -> barcode.rawValue?.let(::pair) }
            .addOnFailureListener { e -> error = scanFailed.replace("%s", e.message ?: "") }
    }

    Page(bottom = {
        Button(
            onClick = ::scan,
            enabled = !busy,
            modifier = Modifier.fillMaxWidth().height(56.dp).testTag("scan_qr"),
        ) {
            Icon(painterResource(R.drawable.ic_qr_scan), null, Modifier.size(22.dp))
            Spacer(Modifier.width(10.dp))
            Text(stringResource(R.string.scan_qr), style = MaterialTheme.typography.titleMedium)
        }
        TextButton(onClick = { showPaste = !showPaste }, modifier = Modifier.testTag("show_paste")) {
            Text(stringResource(R.string.or_paste))
        }
    }) {
        PageHeader(R.string.pair_title, if (setup == Setup.CLOUD) R.string.pair_body_cloud else R.string.pair_body_self, painterResource(R.drawable.ic_qr_scan))
        BrandCard {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
                if (setup == Setup.CLOUD) {
                    NumberedStep(1, stringResource(R.string.pair_cloud_1), stringResource(R.string.pair_cloud_1_body)) {
                        if (BuildConfig.CLOUD_URL.isNotEmpty()) {
                            TextButton(
                                onClick = { ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.CLOUD_URL))) },
                                contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp),
                                modifier = Modifier.testTag("use_cloud"),
                            ) {
                                Text(stringResource(R.string.open_cloud, Uri.parse(BuildConfig.CLOUD_URL).host ?: BuildConfig.CLOUD_URL))
                                Spacer(Modifier.width(4.dp))
                                Icon(painterResource(R.drawable.ic_open_in_new), null, Modifier.size(16.dp))
                            }
                        }
                    }
                    NumberedStep(2, stringResource(R.string.pair_cloud_2), stringResource(R.string.pair_cloud_2_body))
                    NumberedStep(3, stringResource(R.string.pair_scan_step), stringResource(R.string.pair_scan_step_body))
                } else {
                    NumberedStep(1, stringResource(R.string.pair_self_1), stringResource(R.string.pair_self_1_body)) {
                        CommandBlock("docker compose up -d")
                    }
                    NumberedStep(2, stringResource(R.string.pair_self_2), stringResource(R.string.pair_self_2_body)) {
                        CommandBlock("agentring pair")
                    }
                    NumberedStep(3, stringResource(R.string.pair_scan_step), stringResource(R.string.pair_scan_step_body))
                }
            }
        }

        AnimatedVisibility(visible = showPaste || code.isNotEmpty() || error != null) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = code,
                    onValueChange = { code = it },
                    label = { Text(stringResource(R.string.pairing_code)) },
                    minLines = 2,
                    maxLines = 4,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    modifier = Modifier.fillMaxWidth().bringIntoViewRequester(pasteInView).focusRequester(pasteFocus).testTag("pairing_code"),
                )
                OutlinedButton(
                    onClick = { pair(code) },
                    enabled = code.isNotBlank() && !busy,
                    modifier = Modifier.fillMaxWidth().height(48.dp).testTag("pair_button"),
                ) { Text(stringResource(R.string.pair)) }
            }
        }

        if (busy) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                Text(stringResource(R.string.pairing))
            }
        }
        error?.let {
            Surface(color = MaterialTheme.colorScheme.errorContainer, shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth()) {
                Row(Modifier.padding(14.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Icon(Icons.Filled.Warning, null, tint = MaterialTheme.colorScheme.onErrorContainer)
                    Text(it, color = MaterialTheme.colorScheme.onErrorContainer, modifier = Modifier.testTag("pair_error"))
                }
            }
        }
    }
}

@Composable
private fun CallsPage(onNext: () -> Unit) {
    val ready = rememberCallsReady()
    Page(bottom = {
        PrimaryButton(stringResource(if (ready) R.string.continue_ else R.string.calls_later), "calls_next", onClick = onNext)
    }) {
        PageHeader(R.string.calls_title, R.string.calls_body, rememberVectorPainter(Icons.Filled.Notifications))
        CallReadinessCard(showHeader = false)
    }
}

@Composable
private fun DonePage(pairing: Pairing?, onFinished: () -> Unit) {
    val ctx = LocalContext.current
    val cloud = pairing?.isCloud() == true
    Page(bottom = {
        PrimaryButton(stringResource(R.string.open_app), "continue", onClick = onFinished)
    }) {
        HeroBadge(rememberVectorPainter(Icons.Filled.Check), size = 88.dp, pulse = true)
        Text(
            stringResource(R.string.done_title, pairing?.agentName ?: "agentring"),
            style = MaterialTheme.typography.headlineMedium,
            textAlign = TextAlign.Center,
            modifier = Modifier.testTag("title"),
        )
        Text(
            stringResource(R.string.done_subtitle),
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        if (pairing != null && pairing.pushConfig == null) {
            Surface(color = MaterialTheme.colorScheme.errorContainer, shape = MaterialTheme.shapes.medium) {
                Row(Modifier.padding(14.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Icon(Icons.Filled.Warning, null, tint = MaterialTheme.colorScheme.onErrorContainer)
                    Text(stringResource(R.string.done_no_push), color = MaterialTheme.colorScheme.onErrorContainer, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        BrandCard {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
                Text(stringResource(R.string.try_it), style = MaterialTheme.typography.titleMedium)
                if (cloud) {
                    NumberedStep(1, stringResource(R.string.try_cloud_1), stringResource(R.string.try_cloud_1_body))
                    NumberedStep(2, stringResource(R.string.try_cloud_2), stringResource(R.string.try_cloud_2_body))
                } else {
                    NumberedStep(1, stringResource(R.string.try_self_1)) { CommandBlock("agentring msg \"hello\"") }
                    NumberedStep(2, stringResource(R.string.try_self_2)) { CommandBlock("agentring call \"test\"") }
                    NumberedStep(3, stringResource(R.string.try_self_3), stringResource(R.string.try_self_3_body)) {
                        CommandBlock("claude mcp add agentring -- agentring mcp")
                    }
                }
            }
        }
        OutlinedButton(onClick = { Notifications.showTest(ctx) }, modifier = Modifier.fillMaxWidth().testTag("test_notification")) {
            Text(stringResource(R.string.test_notification))
        }
    }
}
