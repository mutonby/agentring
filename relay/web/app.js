// agentring browser client: pairs this browser as a device (platform "web") and
// does what the Android app does for calls, while the tab is open: ring on
// `call.invite`, answer / decline, call the agent, join a group call in progress.
// Events come over the relay WebSocket (no push); audio goes through LiveKit.
import { Room, RoomEvent, Track } from "./vendor/livekit-client.esm.mjs";

const VERSION = "0.2.0-web";
const STORE_KEY = "agentring.web";
/** The relay's LiveKit identity for the agent (`identityOf` in src/routes/calls.ts). */
const AGENT_IDENTITY = "agent";
/** Calling the agent: give up if it hasn't picked up by then (as the app does). */
const OUTGOING_GIVE_UP_MS = 75_000;

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ state

let creds = loadCreds(); // { device_id, device_jwt, relay_id, agent_name, device_name, last_seq }
let agentOnline = false;
let linkUp = false;
/** The one call this browser is showing: phase incoming | outgoing | connecting | active | ended. */
let call = null;
/** A group call from the agent in progress that this browser is not on. */
let joinOffer = null;
let notice = "";
let socket = null;
let reconnectDelay = 1000;
const seen = new Set();

function loadCreds() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "null");
  } catch {
    return null;
  }
}

function saveCreds() {
  try {
    if (creds) localStorage.setItem(STORE_KEY, JSON.stringify(creds));
    else localStorage.removeItem(STORE_KEY);
  } catch {
    // Private mode: pairing then lasts as long as the tab.
  }
}

// ------------------------------------------------------------------ relay API

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(method, path, body) {
  const headers = {};
  if (creds) headers.authorization = `Bearer ${creds.device_jwt}`;
  if (body !== undefined) headers["content-type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 204) return null;
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && creds) unpaired("This browser is no longer paired with the relay.");
    throw new ApiError(res.status, json.error?.code ?? "error", json.error?.message ?? res.statusText);
  }
  return json;
}

// ------------------------------------------------------------------ pairing

async function pair(code, name) {
  let token = code.trim();
  if (token.startsWith("{")) {
    let payload;
    try {
      payload = JSON.parse(token);
    } catch {
      throw new Error("That doesn't look like a pairing code.");
    }
    token = payload.pairing_token ?? "";
    if (payload.relay_url && new URL(payload.relay_url).origin !== location.origin) {
      throw new Error(`This code is for ${payload.relay_url}; open the web client there instead.`);
    }
  }
  if (!token.startsWith("pt_")) throw new Error("That doesn't look like a pairing code.");
  creds = null;
  const res = await api("POST", "/v1/pair", { pairing_token: token, device_name: name, platform: "web", app_version: VERSION });
  creds = {
    device_id: res.device_id,
    device_jwt: res.device_jwt,
    relay_id: res.relay_id,
    agent_name: res.agent_name,
    device_name: name,
    last_seq: 0,
  };
  // Start the event stream from now on: the relay's history is not news to a new device.
  const page = await api("GET", "/v1/events?limit=1");
  creds.last_seq = page.last_seq ?? 0;
  saveCreds();
  start();
}

function unpaired(message) {
  creds = null;
  saveCreds();
  socket?.close();
  socket = null;
  if (call) endCall(null, { silent: true });
  joinOffer = null;
  notice = message ?? "";
  render();
}

// ------------------------------------------------------------------ events

function start() {
  notice = "";
  render();
  api("GET", "/v1/status")
    .then((st) => {
      agentOnline = st.agent_online;
      creds.agent_name = st.agent_name;
      saveCreds();
      const c = st.active_call;
      if (c && c.direction === "agent_to_user" && !call) offerJoin(c);
      render();
    })
    .catch(() => undefined);
  connectSocket();
}

function connectSocket() {
  if (!creds) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/v1/ws?after_seq=${creds.last_seq ?? 0}`;
  // Browsers can't set Authorization on a WebSocket: the token goes as a subprotocol.
  const ws = new WebSocket(url, ["agentring.v1", `bearer.${creds.device_jwt}`]);
  socket = ws;
  ws.onopen = () => {
    linkUp = true;
    reconnectDelay = 1000;
    render();
  };
  ws.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data));
    } catch (e) {
      console.warn("bad event", e);
    }
  };
  ws.onclose = (e) => {
    if (socket !== ws) return;
    socket = null;
    linkUp = false;
    agentOnline = false;
    if (e.code === 4401) {
      unpaired("This browser was unpaired (revoked on the relay).");
      return;
    }
    render();
    setTimeout(connectSocket, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15_000);
  };
}

function onEvent(e) {
  if (typeof e.seq === "number" && e.seq > (creds?.last_seq ?? 0)) {
    creds.last_seq = e.seq;
    saveCreds();
  }
  if (seen.has(e.id)) return;
  seen.add(e.id);
  const mine = call?.id === e.call_id;
  switch (e.type) {
    case "agent.presence":
      agentOnline = e.online;
      break;
    case "call.invite":
      if (e.direction === "agent_to_user") void onInvite(e);
      break;
    case "call.accept":
      if (mine && call.phase === "outgoing" && e.by === "agent") goActive();
      else if (!mine && e.by === "user" && e.device_id !== creds.device_id) void recheckJoin(e.call_id);
      break;
    case "call.decline":
      if (mine && call.phase === "outgoing" && e.by === "agent") endCall("The agent declined the call.");
      break;
    case "call.end":
      if (mine) endCall(call.phase === "outgoing" ? "The call was canceled." : "Call ended.");
      if (joinOffer?.callId === e.call_id) joinOffer = null;
      break;
    case "call.missed":
      if (mine) endCall(call.phase === "outgoing" ? "The agent didn't answer." : `Missed call from ${call.agentName}.`);
      if (joinOffer?.callId === e.call_id) joinOffer = null;
      break;
    default:
      return;
  }
  render();
}

// ------------------------------------------------------------------ incoming calls

async function onInvite(invite) {
  if (Date.now() > invite.expires_at) return; // a replayed, long-gone invite
  if (call && call.phase !== "ended") return; // one call at a time
  // A replay after a reconnect may carry an invite that was answered or canceled since.
  const c = await api("GET", `/v1/calls/${invite.call_id}`).catch(() => null);
  const ringing = c && (c.state === "ringing" || (c.state === "active" && Date.now() < invite.expires_at));
  if (!ringing || (call && call.phase !== "ended")) return;
  joinOffer = null;
  call = {
    id: invite.call_id,
    direction: "agent_to_user",
    agentName: invite.agent_name,
    reason: invite.reason,
    urgent: invite.priority === "urgent",
    phase: "incoming",
  };
  ringer.start(call.urgent);
  notifyIncoming(call);
  api("POST", `/v1/calls/${call.id}/ringing`).catch(() => undefined);
  const id = call.id;
  call.timeout = setTimeout(async () => {
    if (call?.id !== id || call.phase !== "incoming") return;
    endCall(`Missed call from ${call.agentName}.`);
    await recheckJoin(id); // another device may have answered: offer to join instead
  }, invite.expires_at - Date.now() + 2000);
  render();
}

async function answer() {
  const c = call;
  if (c?.phase !== "incoming") return;
  stopRinging();
  c.phase = "connecting";
  render();
  try {
    let res;
    try {
      res = await api("POST", `/v1/calls/${c.id}/accept`);
    } catch (e) {
      // The ring window closed meanwhile, but the call is on: join it instead.
      if (!(e instanceof ApiError) || e.status !== 409) throw e;
      res = await api("POST", `/v1/calls/${c.id}/join`);
    }
    await connectRoom(c, res.join);
    if (call === c) goActive();
  } catch (e) {
    if (call === c) endCall(`Couldn't answer: ${e.message}`);
  }
}

async function decline() {
  const c = call;
  if (c?.phase !== "incoming") return;
  endCall(null);
  const res = await api("POST", `/v1/calls/${c.id}/decline`).catch(() => null);
  if (res?.state === "active") offerJoin(res); // someone else is on it
  render();
}

// ------------------------------------------------------------------ calling the agent

async function callAgent() {
  if (call && call.phase !== "ended") return;
  notice = "";
  $("call-agent").disabled = true;
  let res;
  try {
    res = await api("POST", "/v1/calls", {});
  } catch (e) {
    notice =
      e.code === "agent_offline" ? "The agent is not connected." :
      e.code === "busy" ? "Another call is ringing or in progress." :
      e.code === "calls_unavailable" ? "Calls are not configured on this relay." :
      `Couldn't call: ${e.message}`;
    render();
    return;
  }
  const c = {
    id: res.call.call_id,
    direction: "user_to_agent",
    agentName: res.call.agent_name || creds.agent_name,
    reason: "",
    phase: "outgoing",
    startedAt: Date.now(),
  };
  call = c;
  joinOffer = null;
  render();
  try {
    await connectRoom(c, res.join);
  } catch (e) {
    await api("POST", `/v1/calls/${c.id}/end`).catch(() => undefined);
    if (call === c) endCall(`Couldn't connect: ${e.message}`);
    return;
  }
  // Events of calls to the agent are not pushed and the socket may be down: poll too.
  c.poll = setInterval(async () => {
    if (call !== c || c.phase !== "outgoing") return clearInterval(c.poll);
    if (Date.now() - c.startedAt > OUTGOING_GIVE_UP_MS) {
      await api("POST", `/v1/calls/${c.id}/end`).catch(() => undefined);
      return endCall("The agent didn't answer.");
    }
    const s = await api("GET", `/v1/calls/${c.id}`).catch(() => null);
    if (call !== c || !s) return;
    if (s.state === "active") goActive();
    else if (s.state === "declined") endCall("The agent declined the call.");
    else if (s.state === "missed") endCall("The agent didn't answer.");
    else if (s.state !== "ringing") endCall("Call ended.");
  }, 2000);
}

// ------------------------------------------------------------------ joining a call in progress

function offerJoin(c) {
  joinOffer = { callId: c.call_id, agentName: c.agent_name, reason: c.reason ?? "" };
  render();
}

async function recheckJoin(callId) {
  if (call && call.phase !== "ended") return;
  const c = await api("GET", `/v1/calls/${callId}`).catch(() => null);
  if (c?.state === "active" && c.direction === "agent_to_user") offerJoin(c);
  else if (joinOffer?.callId === callId) joinOffer = null;
  render();
}

async function join() {
  const offer = joinOffer;
  if (!offer || (call && call.phase !== "ended")) return;
  joinOffer = null;
  const c = { id: offer.callId, direction: "agent_to_user", agentName: offer.agentName, reason: offer.reason, phase: "connecting" };
  call = c;
  render();
  try {
    const res = await api("POST", `/v1/calls/${c.id}/join`);
    await connectRoom(c, res.join);
    if (call === c) goActive();
  } catch (e) {
    if (call === c) endCall(e.code === "call_ended" ? "That call is over." : `Couldn't join: ${e.message}`);
  }
}

// ------------------------------------------------------------------ the call itself

async function connectRoom(c, joinInfo) {
  const room = new Room({
    adaptiveStream: false,
    dynacast: false,
    audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    // Voice, not music: as the Android app, 32 kbps Opus with DTX and redundancy (RED).
    publishDefaults: { dtx: true, red: true, audioPreset: { maxBitrate: 32_000 } },
  });
  c.room = room;
  room
    .on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === Track.Kind.Audio) $("audio").append(track.attach());
    })
    .on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()))
    .on(RoomEvent.ParticipantConnected, (p) => {
      if (p.identity === AGENT_IDENTITY && call === c && c.phase === "outgoing") goActive();
      render();
    })
    .on(RoomEvent.ParticipantDisconnected, (p) => {
      if (p.identity === AGENT_IDENTITY && call === c && c.phase !== "ended") endCall("The agent hung up.");
      else render();
    })
    .on(RoomEvent.Reconnecting, () => {
      c.reconnecting = true;
      render();
    })
    .on(RoomEvent.Reconnected, () => {
      c.reconnecting = false;
      render();
    })
    .on(RoomEvent.Disconnected, () => {
      if (call === c && c.phase !== "ended") endCall("Disconnected from the call.");
    })
    .on(RoomEvent.AudioPlaybackStatusChanged, render);
  await room.connect(joinInfo.livekit_url, joinInfo.livekit_token);
  if (call !== c) {
    room.disconnect();
    return;
  }
  try {
    await room.localParticipant.setMicrophoneEnabled(true);
  } catch (e) {
    console.warn("no microphone", e);
    c.noMic = true;
  }
  // The agent may have joined before us: no ParticipantConnected then.
  if (c.phase === "outgoing" && room.remoteParticipants.has(AGENT_IDENTITY)) goActive();
  render();
}

function goActive() {
  if (!call || call.phase === "active" || call.phase === "ended") return;
  stopRinging();
  call.phase = "active";
  call.answeredAt = Date.now();
  clearInterval(call.poll);
  call.ticker = setInterval(render, 1000);
  render();
}

async function hangUp() {
  const c = call;
  if (!c || c.phase === "ended") return;
  if (c.phase === "incoming") return decline();
  endCall(null);
  const res = await api("POST", `/v1/calls/${c.id}/end`).catch(() => null);
  // Leaving a group call: the others may still be on it, so this browser can rejoin.
  if (res?.state === "active" && res.direction === "agent_to_user") offerJoin(res);
  render();
}

function toggleMute() {
  const c = call;
  if (!c?.room || c.noMic) return;
  c.muted = !c.muted;
  c.room.localParticipant.setMicrophoneEnabled(!c.muted).catch(() => undefined);
  render();
}

/** Tears the call down locally. `message` = what to show on the home screen afterwards. */
function endCall(message, { silent = false } = {}) {
  const c = call;
  if (!c) return;
  c.phase = "ended";
  stopRinging();
  clearTimeout(c.timeout);
  clearInterval(c.poll);
  clearInterval(c.ticker);
  c.room?.disconnect();
  $("audio").replaceChildren();
  call = null;
  if (!silent) notice = message ?? "";
  render();
}

// ------------------------------------------------------------------ ringing

/** A ringtone made with WebAudio (no file to fetch). Browsers only allow sound after a click. */
const ringer = {
  ctx: null,
  timer: null,
  nodes: [],
  unlock() {
    if (!this.ctx) this.ctx = new AudioContext();
    if (this.ctx.state === "suspended") this.ctx.resume().catch(() => undefined);
  },
  start(urgent) {
    this.stop();
    if (!this.ctx) return; // no click on this page yet: the notification and the title still show it
    const [on, off] = urgent ? [1000, 600] : [2000, 4000];
    const burst = () => {
      const t = this.ctx.currentTime;
      const gain = this.ctx.createGain();
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.15, t + 0.05);
      gain.gain.setValueAtTime(0.15, t + on / 1000 - 0.05);
      gain.gain.linearRampToValueAtTime(0, t + on / 1000);
      gain.connect(this.ctx.destination);
      for (const f of [440, 480]) {
        const osc = this.ctx.createOscillator();
        osc.frequency.value = f;
        osc.connect(gain);
        osc.start(t);
        osc.stop(t + on / 1000);
        this.nodes.push(osc);
      }
    };
    burst();
    this.timer = setInterval(burst, on + off);
  },
  stop() {
    clearInterval(this.timer);
    this.timer = null;
    for (const n of this.nodes) {
      try {
        n.stop();
      } catch {
        // already stopped
      }
    }
    this.nodes = [];
  },
};

let notification = null;

function notifyIncoming(c) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
  notification = new Notification(`${c.agentName} is calling`, {
    body: c.reason || "Incoming call",
    tag: c.id,
    requireInteraction: true,
  });
  notification.onclick = () => window.focus();
}

function stopRinging() {
  ringer.stop();
  notification?.close();
  notification = null;
}

// ------------------------------------------------------------------ rendering

const initial = (name) => (name || "?").trim().charAt(0).toUpperCase() || "?";

function duration(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function render() {
  $("insecure").hidden = window.isSecureContext;
  const paired = creds !== null;
  const inCall = call !== null && call.phase !== "ended";
  $("pair").hidden = paired;
  $("home").hidden = !paired || inCall;
  $("call").hidden = !inCall;
  $("link").hidden = !paired;
  $("link").textContent = linkUp ? "connected" : "reconnecting…";
  $("link").classList.toggle("down", !linkUp);

  if (!paired) {
    $("pair-error").hidden = !notice;
    $("pair-error").textContent = notice;
    document.title = "agentring";
    return;
  }

  const agentName = creds.agent_name || "Agent";
  $("home-avatar").textContent = initial(agentName);
  $("home-agent").textContent = agentName;
  $("presence-dot").classList.toggle("on", agentOnline);
  $("presence").textContent = agentOnline ? "online" : "offline";
  $("call-agent").disabled = !agentOnline || !linkUp || inCall;
  $("home-notice").hidden = !notice;
  $("home-notice").textContent = notice;
  $("join-offer").hidden = !joinOffer;
  if (joinOffer) $("join-text").textContent = `A call with ${joinOffer.agentName} is in progress.`;
  $("device-name").textContent = creds.device_name ?? "";
  $("notify").hidden = !("Notification" in window) || Notification.permission !== "default";

  if (!inCall) {
    document.title = "agentring";
    return;
  }
  const people = call.room
    ? [...call.room.remoteParticipants.values()].filter((p) => p.identity !== AGENT_IDENTITY).length
    : 0;
  const status =
    call.reconnecting ? "Reconnecting…" :
    call.phase === "incoming" ? (call.urgent ? "Urgent call" : "Incoming call") :
    call.phase === "outgoing" ? "Calling…" :
    call.phase === "connecting" ? "Connecting…" :
    duration(Date.now() - call.answeredAt);
  $("call").classList.toggle("ringing", call.phase === "incoming" || call.phase === "outgoing");
  $("call-avatar").textContent = initial(call.agentName);
  $("call-agent-name").textContent = call.agentName;
  $("call-status").textContent = status;
  $("call-reason").hidden = !call.reason;
  $("call-reason").textContent = call.reason;
  $("call-people").hidden = people === 0;
  $("call-people").textContent = people === 1 ? "1 other person on the call" : `${people} other people on the call`;
  $("call-mic").hidden = !call.noMic;
  $("start-audio").hidden = !call.room || call.room.canPlaybackAudio;
  $("ringing-controls").hidden = call.phase !== "incoming";
  $("active-controls").hidden = call.phase === "incoming";
  $("mute").disabled = !call.room || call.noMic || call.phase === "connecting";
  $("mute").setAttribute("aria-pressed", String(!!call.muted));
  $("mute").textContent = call.muted ? "Unmute" : "Mute";
  $("hangup").textContent = call.phase === "outgoing" ? "Cancel" : "Hang up";
  document.title = call.phase === "incoming" ? `📞 ${call.agentName} is calling` : `${status} · agentring`;
}

// ------------------------------------------------------------------ wiring

document.addEventListener("pointerdown", () => ringer.unlock(), { capture: true });
document.addEventListener("keydown", () => ringer.unlock(), { capture: true });

$("pair-name").value = /Mac/.test(navigator.userAgent) ? "Browser on Mac" : /Win/.test(navigator.userAgent) ? "Browser on Windows" : "Browser";
$("pair-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter;
  button.disabled = true;
  notice = "";
  try {
    await pair($("pair-code").value, $("pair-name").value.trim() || "Browser");
    $("pair-code").value = "";
  } catch (e) {
    notice =
      e.code === "invalid_pairing_token" ? "That code is invalid, expired or already used. Run `agentring pair` again." :
      e.code === "rate_limited" ? "Too many attempts; wait a minute." :
      e.message;
    creds = null;
    render();
  } finally {
    button.disabled = false;
  }
});
$("call-agent").addEventListener("click", () => void callAgent());
$("join").addEventListener("click", () => void join());
$("answer").addEventListener("click", () => void answer());
$("decline").addEventListener("click", () => void decline());
$("hangup").addEventListener("click", () => void hangUp());
$("mute").addEventListener("click", toggleMute);
$("start-audio").addEventListener("click", () => call?.room?.startAudio().then(render));
$("notify").addEventListener("click", () => Notification.requestPermission().then(render));
$("unpair").addEventListener("click", () => {
  const revoke = `agentring devices revoke ${creds?.device_id ?? ""}`;
  if (!confirm(`Forget this browser here? To revoke its credentials on the relay too, run: ${revoke}`)) return;
  unpaired("");
});
window.addEventListener("beforeunload", (ev) => {
  if (call && call.phase !== "ended") ev.preventDefault(); // leaving would drop the call
});

if (creds) start();
render();
