# Android Auto

With the phone connected to Android Auto, agentring shows up in the car's app launcher
with one screen: **Call &lt;your agent&gt;**. Tap it and the call starts. Calls in both
directions (the agent calling you, you calling the agent) use Android Auto's own in-call
view, where you answer, decline, mute and hang up, with the audio on the car's speakers
and microphone.

Status: **beta**, like Google's support for calling apps in cars. It is built on Google's
[calling apps for cars](https://developer.android.com/training/cars/communication/calling)
guide. Tested with the Desktop Head Unit and a Pixel 6 (Android 16) with the phone
locked: calling the agent from the car, an incoming call answered in the car, mute, and
hanging up from the car. Not yet tested in a real car.

## Which builds have it

Google Play only accepts calling apps for cars on its internal and closed testing tracks.
It rejects a production or open-testing release that contains one. So:

| Build | Android Auto |
|---|---|
| Google Play (production) | not included |
| A Play closed-testing track, if we open one | included |
| The APK on [GitHub Releases](https://github.com/mutonby/agentring/releases) | included |
| `android-preview` and any debug build | included |

The APK on Releases is signed with a different key from the Play app, so uninstall one
before installing the other. You have to pair the phone again afterwards.

## Using it with the APK from GitHub

Android Auto only lists apps installed from Google Play, unless you allow other sources
once on the phone:

1. Open Android Auto's settings. Depending on the phone, that is the Android Auto app or
   **Settings → Connected devices → Connection preferences → Android Auto**.
2. Scroll to **Version** and tap it about ten times until it asks to enable developer
   settings. Accept.
3. In the menu (⋮), open **Developer settings** and turn on **Unknown sources**.
4. Connect to the car (or start the head unit) again. agentring is now in the launcher.

In the agentring app, keep **Settings → Use Android's call system** on (it is on by
default). Without it, calls don't reach the car.

## What works

- **Call the agent from the car.** The screen says whether the agent is online. If the
  phone isn't paired yet, or the microphone permission is missing, the screen says so.
  You fix it on the phone when it is safe.
- **Calls from the agent** ring in the car, and you answer or decline them there.
- **In-call controls.** Mute, unmute and hang up from the car. The phone's call screen
  follows the car's mute.

Not yet: messages in the car, "Hey Google, call my agent", and choosing among several
agents.

## Testing with the Desktop Head Unit

1. In Android Studio's SDK Manager (SDK Tools tab), install **Android Auto Desktop Head
   Unit Emulator**. It goes in `$ANDROID_HOME/extras/google/auto/`.
2. Install a debug build on the phone (`./gradlew :app:installDebug`). Debug builds
   accept any head unit.
3. In Android Auto's developer settings (above), choose **Start head unit server**.
4. On the computer, run:

   ```bash
   adb forward tcp:5277 tcp:5277
   $ANDROID_HOME/extras/google/auto/desktop-head-unit
   ```

5. Open agentring in the head unit and tap **Call &lt;agent&gt;**. To test an incoming
   call, run `agentring call "test from the car"` on the computer.

## For developers

- The car screen lives in `app/src/main/kotlin/dev/agentring/app/car/`. It is a
  `CarAppService` in the `androidx.car.app.category.CALLING` category, with a single
  `ListTemplate`.
- It is declared in the manifest of the `:auto` module, which has no code. Debug builds
  always depend on it. Release builds depend on it only with `-PagentringAndroidAuto=true`.
  The release workflow passes that flag for the GitHub APK and checks the manifest.
  Plain `bundleRelease` for Play leaves it out.
- Calls reach the car because they are Telecom calls (`TelecomBridge`, Jetpack
  Core-Telecom). Nothing car-specific is needed for answering or hanging up. The mute
  button in the car changes Telecom's mute, and `CallManager` applies it to the call.
