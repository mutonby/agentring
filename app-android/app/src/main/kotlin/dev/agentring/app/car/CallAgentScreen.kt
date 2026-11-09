package dev.agentring.app.car

import androidx.car.app.CarContext
import androidx.car.app.CarToast
import androidx.car.app.Screen
import androidx.car.app.model.Action
import androidx.car.app.model.CarIcon
import androidx.car.app.model.ItemList
import androidx.car.app.model.ListTemplate
import androidx.car.app.model.MessageTemplate
import androidx.car.app.model.Row
import androidx.car.app.model.Template
import androidx.core.graphics.drawable.IconCompat
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.app.call.CallPermissions
import dev.agentring.app.call.CallUi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The car's one screen: "Call <agent>". Once the call is placed, Android Auto shows its
 * own in-call view over this screen. Things that need the phone (pairing, the microphone
 * permission) only get a short note: the driver mustn't be sent to the phone while driving.
 */
internal class CallAgentScreen(carContext: CarContext) : Screen(carContext) {
    private var placing = false
    private var error: Int? = null
    /** From the relay's status when the screen starts; null until known. */
    private var agentOnline: Boolean? = null

    init {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                combine(Graph.calls.state, Graph.pairing.revoked) { _, _ -> }.collect { refresh() }
            }
        }
        lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) = checkAgent()
        })
    }

    override fun onGetTemplate(): Template {
        val pairing = Graph.pairing.current
        if (pairing == null || Graph.pairing.revoked.value) {
            return MessageTemplate.Builder(carContext.getString(R.string.car_not_paired))
                .setTitle(carContext.getString(R.string.app_name))
                .setHeaderAction(Action.APP_ICON)
                .build()
        }
        val call = Graph.calls.state.value
        val onCall = call != null && call !is CallUi.Ended
        val status = when {
            placing -> carContext.getString(R.string.call_calling, pairing.agentName)
            onCall -> carContext.getString(R.string.call_agent_in_call)
            error != null -> carContext.getString(error!!)
            agentOnline == false -> carContext.getString(R.string.call_agent_offline)
            else -> carContext.getString(R.string.call_agent_hint, pairing.agentName)
        }
        val row = Row.Builder()
            .setTitle(carContext.getString(R.string.call_agent_named, pairing.agentName))
            .addText(status)
            .setImage(CarIcon.Builder(IconCompat.createWithResource(carContext, R.drawable.ic_call)).build())
            .setOnClickListener(::place)
            .build()
        @Suppress("DEPRECATION") // setHeader needs car API level 7; setTitle works on every host.
        return ListTemplate.Builder()
            .setTitle(carContext.getString(R.string.app_name))
            .setHeaderAction(Action.APP_ICON)
            .setSingleList(ItemList.Builder().addItem(row).build())
            .build()
    }

    private fun place() {
        val call = Graph.calls.state.value
        if (placing || (call != null && call !is CallUi.Ended)) return
        if (!CallPermissions.hasMic(carContext)) {
            show(R.string.car_needs_mic)
            return
        }
        placing = true
        error = null
        invalidate()
        // Not the screen's scope: leaving the screen mid-request must not orphan a ringing call.
        Graph.appScope.launch(Dispatchers.Main.immediate) {
            val failure = Graph.calls.callAgent()
            placing = false
            if (failure != null) show(failure) else refresh()
        }
    }

    private fun show(message: Int) {
        error = message
        if (!alive()) return
        CarToast.makeText(carContext, message, CarToast.LENGTH_LONG).show()
        invalidate()
    }

    /** Whether the agent is online, so the screen says so before the driver taps. */
    private fun checkAgent() {
        lifecycleScope.launch {
            val online = withContext(Dispatchers.IO) { runCatching { Graph.api()?.status()?.agentOnline }.getOrNull() }
            if (online != agentOnline) {
                agentOnline = online
                if (online == true && error == R.string.call_agent_offline) error = null
                refresh()
            }
        }
    }

    private fun refresh() {
        if (alive()) invalidate()
    }

    private fun alive() = lifecycle.currentState.isAtLeast(Lifecycle.State.CREATED)
}
