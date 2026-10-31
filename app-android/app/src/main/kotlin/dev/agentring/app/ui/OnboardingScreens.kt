package dev.agentring.app.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Phone
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import dev.agentring.app.BuildConfig
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.app.data.Pairing
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private enum class Step { WELCOME, NOTIFICATIONS, PAIR, DONE }

@Composable
fun OnboardingFlow(paired: Pairing?, onFinished: () -> Unit) {
    val ctx = LocalContext.current
    var step by rememberSaveable { mutableStateOf(if (paired != null) Step.DONE else Step.WELCOME) }
    val needsNotificationPermission = remember {
        Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        step = Step.PAIR
    }

    Scaffold { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(24.dp)
                .verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Spacer(Modifier.height(32.dp))
            when (step) {
                Step.WELCOME -> {
                    Icon(Icons.Filled.Phone, null, Modifier.size(72.dp), tint = MaterialTheme.colorScheme.primary)
                    Title(stringResource(R.string.welcome_title))
                    Body(stringResource(R.string.welcome_body))
                    Button(
                        onClick = { step = if (needsNotificationPermission) Step.NOTIFICATIONS else Step.PAIR },
                        modifier = Modifier.fillMaxWidth().testTag("get_started"),
                    ) { Text(stringResource(R.string.get_started)) }
                }
                Step.NOTIFICATIONS -> {
                    Icon(Icons.Filled.Notifications, null, Modifier.size(72.dp), tint = MaterialTheme.colorScheme.primary)
                    Title(stringResource(R.string.notif_title))
                    Body(stringResource(R.string.notif_body))
                    Button(
                        onClick = { if (Build.VERSION.SDK_INT >= 33) permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text(stringResource(R.string.allow)) }
                    TextButton(onClick = { step = Step.PAIR }) { Text(stringResource(R.string.skip)) }
                }
                Step.PAIR -> PairStep(onPaired = { step = Step.DONE })
                Step.DONE -> {
                    val p = paired
                    Title(stringResource(R.string.done_title, p?.agentName ?: ""))
                    Body(stringResource(R.string.done_body))
                    if (p != null && p.pushConfig == null) {
                        Text(
                            stringResource(R.string.done_no_push),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                            textAlign = TextAlign.Center,
                        )
                    }
                    CallReadinessCard()
                    Button(onClick = onFinished, modifier = Modifier.fillMaxWidth().testTag("continue")) {
                        Text(stringResource(R.string.continue_))
                    }
                }
            }
        }
    }
}

@Composable
private fun PairStep(onPaired: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var code by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

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

    Icon(Icons.Filled.Search, null, Modifier.size(72.dp), tint = MaterialTheme.colorScheme.primary)
    Title(stringResource(R.string.pair_title))
    Body(stringResource(R.string.pair_body))

    val scanFailed = stringResource(R.string.scan_failed, "%s")
    Button(
        onClick = {
            val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
            GmsBarcodeScanning.getClient(ctx, options).startScan()
                .addOnSuccessListener { barcode -> barcode.rawValue?.let(::pair) }
                .addOnFailureListener { e -> error = scanFailed.replace("%s", e.message ?: "") }
        },
        enabled = !busy,
        modifier = Modifier.fillMaxWidth().testTag("scan_qr"),
    ) { Text(stringResource(R.string.scan_qr)) }

    Text(stringResource(R.string.or_paste), style = MaterialTheme.typography.bodySmall)
    OutlinedTextField(
        value = code,
        onValueChange = { code = it },
        label = { Text(stringResource(R.string.pairing_code)) },
        minLines = 2,
        maxLines = 4,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
        modifier = Modifier.fillMaxWidth().testTag("pairing_code"),
    )
    OutlinedButton(
        onClick = { pair(code) },
        enabled = code.isNotBlank() && !busy,
        modifier = Modifier.fillMaxWidth().testTag("pair_button"),
    ) { Text(stringResource(R.string.pair)) }

    if (BuildConfig.CLOUD_URL.isNotEmpty()) {
        TextButton(
            onClick = { ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.CLOUD_URL))) },
            modifier = Modifier.testTag("use_cloud"),
        ) { Text(stringResource(R.string.use_cloud)) }
    }

    if (busy) {
        CircularProgressIndicator()
        Text(stringResource(R.string.pairing))
    }
    error?.let {
        Text(it, color = MaterialTheme.colorScheme.error, textAlign = TextAlign.Center, modifier = Modifier.testTag("pair_error"))
    }
}

@Composable
private fun Title(text: String) =
    Text(text, style = MaterialTheme.typography.headlineSmall, textAlign = TextAlign.Center, modifier = Modifier.testTag("title"))

@Composable
private fun Body(text: String) = Text(text, style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center)
