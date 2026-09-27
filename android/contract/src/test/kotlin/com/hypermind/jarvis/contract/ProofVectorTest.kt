package com.hypermind.jarvis.contract

import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyFactory
import java.security.Signature
import java.security.spec.EdECPrivateKeySpec
import java.security.spec.NamedParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.UUID

/**
 * The device's signed formats, reproduced byte for byte from
 * `shared/android/proof_vectors.json`, which the server generates with its own
 * format functions and verifies with its own verifier. Ed25519 is
 * deterministic, so equal strings mean equal formats.
 */
class ProofVectorTest {
    private val vector = ContractJson.parseToJsonElement(Shared.read("proof_vectors.json")).jsonObject

    private fun field(name: String) = vector.getValue(name).jsonPrimitive

    private val signer =
        Signer { message ->
            val key =
                KeyFactory
                    .getInstance("Ed25519")
                    .generatePrivate(
                        EdECPrivateKeySpec(NamedParameterSpec.ED25519, DeviceProof.unb64(field("seed_b64").content)),
                    )
            Signature.getInstance("Ed25519").run {
                initSign(key)
                update(message)
                sign()
            }
        }

    @Test
    fun `the device proof matches the server's format exactly`() {
        val proof =
            DeviceProof.proof(
                signer,
                UUID.fromString(field("device_id").content),
                field("issued_at").long,
                nonce = field("nonce").content,
            )
        assertEquals(field("proof").content, proof)
    }

    @Test
    fun `the registration proof of possession matches`() {
        val signature =
            DeviceProof.registrationSignature(
                signer,
                field("bootstrap_token").content,
                field("public_key_b64").content,
            )
        assertEquals(field("registration_signature").content, signature)
    }

    @Test
    fun `the rotation proof matches`() {
        val signature =
            DeviceProof.rotationSignature(
                signer,
                UUID.fromString(field("device_id").content),
                field("public_key_b64").content,
            )
        assertEquals(field("rotation_signature").content, signature)
    }

    @Test
    fun `the step-up message matches the server's format and its signature verifies`() {
        val stepUp = vector.getValue("step_up").jsonObject
        val message =
            StepUp.message(
                deviceId = field("device_id").content,
                challenge = stepUp.getValue("challenge").jsonPrimitive.content,
            )
        assertEquals(stepUp.getValue("message").jsonPrimitive.content, String(message, Charsets.UTF_8))
        val publicKey =
            KeyFactory
                .getInstance("EC")
                .generatePublic(
                    X509EncodedKeySpec(DeviceProof.unb64(stepUp.getValue("public_key_spki_b64").jsonPrimitive.content)),
                )
        val verifier =
            Signature.getInstance("SHA256withECDSA").apply {
                initVerify(publicKey)
                update(message)
            }
        assertTrue(verifier.verify(DeviceProof.unb64(stepUp.getValue("signature_der_b64").jsonPrimitive.content)))
    }
}
