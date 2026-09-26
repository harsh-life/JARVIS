package com.hypermind.jarvis.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

/**
 * The shared capability → operation → primitive table (docs/23 §5.1),
 * loaded from `shared/android/device_mapping.json` — bundled into the build
 * from its one location in the repository, never re-typed here.
 *
 * [load] recomputes the content digest and refuses a table whose
 * `mapping_version` does not match it: a hand-edited or truncated table is not
 * a table this device may act on. A device holding no valid table refuses
 * every operation (fail closed).
 */
class DeviceMapping private constructor(
    val version: String,
    private val capabilities: Map<String, Map<String, PrimitiveSpec>>,
) {
    fun lookup(
        capability: String,
        operation: String,
    ): PrimitiveSpec? = capabilities[capability]?.get(operation)

    val capabilityNames: Set<String> get() = capabilities.keys

    fun operations(capability: String): Set<String> = capabilities[capability]?.keys.orEmpty()

    companion object {
        const val SCHEMA = 1

        fun load(json: String): DeviceMapping {
            val root = ContractJson.parseToJsonElement(json).jsonObject
            val body = JsonObject(root.filterKeys { it != "mapping_version" })
            val document = ContractJson.decodeFromJsonElement(MappingDocument.serializer(), root)
            if (document.schema != SCHEMA) throw MappingIntegrityError("unsupported mapping schema ${document.schema}")
            val expected = "${document.schema}-" + CanonicalJson.sha256Hex(CanonicalJson.encode(body)).take(16)
            if (expected != document.mappingVersion) {
                throw MappingIntegrityError("mapping content does not match its version")
            }
            return DeviceMapping(document.mappingVersion, document.capabilities)
        }
    }
}

class MappingIntegrityError(
    message: String,
) : IllegalStateException(message)

@Serializable
private data class MappingDocument(
    val schema: Int,
    val capabilities: Map<String, Map<String, PrimitiveSpec>>,
    @SerialName("mapping_version") val mappingVersion: String,
)

@Serializable
enum class PackageScope {
    @SerialName("required")
    REQUIRED,

    @SerialName("forbidden")
    FORBIDDEN,
}

@Serializable
enum class ArgumentKind {
    @SerialName("string")
    STRING,

    @SerialName("int")
    INT,

    @SerialName("bool")
    BOOL,

    @SerialName("enum")
    ENUM,
}

@Serializable
data class ArgumentSpec(
    val kind: ArgumentKind,
    val required: Boolean,
    @SerialName("max_length") val maxLength: Int? = null,
    val minimum: Long? = null,
    val maximum: Long? = null,
    val values: List<String> = emptyList(),
)

@Serializable
data class PrimitiveSpec(
    val primitive: String,
    val mechanism: Mechanism,
    @SerialName("grid_toggle") val gridToggle: GridToggle,
    @SerialName("package_scope") val packageScope: PackageScope,
    val dependencies: List<PlatformDependency>,
    val arguments: Map<String, ArgumentSpec>,
    @SerialName("one_of") val oneOf: List<List<String>>,
    @SerialName("max_result_bytes") val maxResultBytes: Int,
    /** The shape of this primitive's `ok` result ([PerceptionCheck]). */
    val result: ResultKind,
)
