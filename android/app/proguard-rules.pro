# Conservative R8 rules for the ISF Recorder (Jomi) app.
# Library code (Compose, Media3, OkHttp, Room runtime, WorkManager, coroutines) is shrunk using
# the consumer rules those libraries ship; ALL app classes are kept unobfuscated.

# App code: keep everything (small; covers Room *_Impl, workers, Gson TypeToken subclasses,
# reflection by class name, data classes, enums).
-keep class com.isf.recorder.** { *; }

# Gson: generic types come from TypeToken anonymous subclasses -> keep generic signatures.
-keepattributes Signature, InnerClasses, EnclosingMethod, *Annotation*
-keep class com.google.gson.reflect.TypeToken { *; }
-keep class * extends com.google.gson.reflect.TypeToken
-keep class com.google.gson.** { *; }

# WorkManager creates workers by class name; Room finds <Db>_Impl by name (also covered above).
-keep class * extends androidx.work.ListenableWorker {
    public <init>(android.content.Context, androidx.work.WorkerParameters);
}
-keep class * extends androidx.room.RoomDatabase { <init>(); }

# Kotlin metadata / coroutines
-keep class kotlin.Metadata { *; }
-dontwarn kotlinx.coroutines.**

# Readable crash traces
-keepattributes SourceFile, LineNumberTable
