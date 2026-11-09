// The Android Auto entry point as a manifest-only library. The car screens live in :app
// (dev.agentring.app.car); this module only declares them, so a build can leave Android
// Auto out by not depending on it. Google Play rejects calling apps for cars on its open
// testing and production tracks (docs/android-auto.md).
plugins {
    alias(libs.plugins.android.library)
}

android {
    namespace = "dev.agentring.app.auto"
    compileSdk = 36

    defaultConfig {
        minSdk = 26
    }
}
