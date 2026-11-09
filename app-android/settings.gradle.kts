// The Android app needs Google's Maven repository. Pass -PcoreOnly to build just
// the pure-Kotlin :core module where that repository is not reachable.
val coreOnly = providers.gradleProperty("coreOnly").isPresent

pluginManagement {
    repositories {
        if (!providers.gradleProperty("coreOnly").isPresent) google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        if (!coreOnly) google()
        mavenCentral()
        if (!coreOnly) {
            // LiveKit's fork of Twilio audioswitch is only published on JitPack.
            // Scoped to that one module so JitPack can't serve anything else.
            exclusiveContent {
                forRepository { maven("https://jitpack.io") }
                filter { includeModule("com.github.davidliu", "audioswitch") }
            }
        }
    }
}

rootProject.name = "agentring-android"
include(":core")
if (!coreOnly) {
    include(":app")
    // Android Auto entry point: only its manifest, so builds can leave it out (docs/android-auto.md).
    include(":auto")
}
