# Readable stack traces once Play has the mapping file.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

# kotlinx.serialization: the relay protocol models.
-keepattributes *Annotation*, InnerClasses, Signature
-dontnote kotlinx.serialization.**
-keepclassmembers @kotlinx.serialization.Serializable class dev.agentring.** {
    *** Companion;
    *** INSTANCE;
    kotlinx.serialization.KSerializer serializer(...);
}
-keepclasseswithmembers class dev.agentring.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class dev.agentring.**$$serializer { *; }

# WebRTC reaches these classes from native code by name; LiveKit's protobufs use reflection.
-keep class org.webrtc.** { *; }
-keep class livekit.** { *; }
-keep class io.livekit.android.** { *; }
