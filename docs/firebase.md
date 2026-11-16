# Push notifications with your own Firebase project

agentring wakes your phone with **Firebase Cloud Messaging (FCM)** high-priority
data messages. You use **your own** free Firebase project: nothing goes through
anyone else's servers, and the prebuilt APK works with it without rebuilding —
the relay hands the app the (public) client settings when you pair.

Without Firebase everything still works while the app is open on screen (it keeps a
WebSocket to the relay), but a locked phone or a closed app won't notice new messages
or **ring for calls**. So for calls, it is not optional.

Time: ~5 minutes. Cost: free (FCM has no usage charges).

## 1. Create the project

1. Open <https://console.firebase.google.com> and click **Create a project**.
2. Name it (e.g. `agentring`). Google Analytics is not needed — turn it off.

## 2. Register the Android app

1. In the project overview click the **Android** icon (*Add app → Android*).
2. **Android package name:** `dev.agentring.app` — exactly this.
3. App nickname: anything. **Leave the SHA-1 empty.**
4. Click **Register app**, then **Download google-services.json**.
5. Skip the remaining "Add Firebase SDK" steps (the app already contains it).

## 3. Create a service account key (lets the relay send pushes)

1. Click the gear ⚙ → **Project settings** → **Service accounts** tab.
2. Click **Generate new private key** → **Generate key**. A JSON file downloads.
3. Treat this file like a password: anyone holding it can send pushes to your
   phone through your project.

> The **Cloud Messaging** tab should show *Firebase Cloud Messaging API (V1): Enabled*
> (the default for new projects). If it says disabled, enable it there.

## 4. Give both files to the relay

On the relay server, with the docker compose deployment:

```bash
cd deploy
cp ~/Downloads/google-services.json        secrets/google-services.json
cp ~/Downloads/agentring-*-firebase-adminsdk-*.json secrets/firebase-service-account.json
chmod 600 secrets/*.json
# The relay runs as uid 1000 inside its container. If your user on the server isn't
# uid 1000 (check with `id -u`), hand the files to that uid so the relay can read them:
sudo chown 1000:1000 secrets/*.json
```

In `deploy/.env` uncomment:

```bash
FCM_SERVICE_ACCOUNT_FILE=/secrets/firebase-service-account.json
FCM_GOOGLE_SERVICES_FILE=/secrets/google-services.json
```

Restart: `docker compose up -d`. The relay log should say `"push":"fcm"`, and
`agentring status` shows `Push: fcm`.

Running the relay without docker? Point the same two environment variables at
the files.

## 5. (Re-)pair the phone

The app learns the Firebase settings when it pairs. If you paired before
configuring Firebase, pair again: in the app *Settings → Unpair this phone*,
then `agentring pair` on your computer. The app's Home screen should show
**Push: Registered**, and `agentring status` shows `push ok` for the device.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Relay keeps restarting with `EACCES: permission denied` on `/secrets/…json` | The files are `chmod 600` and owned by a user other than uid 1000, the relay's user in the container: `sudo chown 1000:1000 deploy/secrets/*.json`. |
| Relay fails to start: `google-services.json has no Android app with package name "dev.agentring.app"` | You registered a different package name. Add another Android app with `dev.agentring.app` and download the new file. |
| Home shows *Push: Unavailable: Google Play services* | The phone has no Google Play services (e.g. some Huawei / de-Googled ROMs). UnifiedPush support is planned. |
| Home shows *Push: Error …* | Check the phone has internet, then reopen the app. Make sure the relay was restarted after adding the files and that you re-paired. |
| `agentring status` says `NO push token` | The app never registered: open it once; check the Home screen's Push row. |
| Pushes arrive late or not at all with the screen off | Battery optimisation: see [troubleshooting-oem.md](troubleshooting-oem.md). |
