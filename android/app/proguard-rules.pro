# kotlinx.serialization: keep generated serializers for the wire contract.
-keepattributes *Annotation*, InnerClasses
-keepclassmembers class com.hypermind.jarvis.contract.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.hypermind.jarvis.contract.**$$serializer { *; }
