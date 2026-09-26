package com.hypermind.jarvis.auth

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.google.crypto.tink.subtle.Ed25519Verify
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Instrumentation (real Android Keystore — needs a device or emulator):
 * the key is created inside the Keystore where the platform allows, signs
 * verifiable Ed25519 signatures, survives a reload, and is destroyed on
 * revocation. The protection level actually obtained is reported.
 */
@RunWith(AndroidJUnit4::class)
class KeystoreDeviceKeyStoreTest {
    private val store = KeystoreDeviceKeyStore(ApplicationProvider.getApplicationContext())

    @After
    fun cleanUp() = store.destroy()

    @Test
    fun keyIsCreatedSignsAndReloads() {
        val key = store.create()
        assertEquals(32, key.publicKey.size)
        val message = "hypermind-device-proof|v1|x|n|1".toByteArray()
        Ed25519Verify(key.publicKey).verify(key.sign(message), message)
        val reloaded = assertNotNull(store.load()).let { store.load()!! }
        Ed25519Verify(key.publicKey).verify(reloaded.sign(message), message)
        android.util.Log.i("JarvisKeyTest", "key protection on this device: ${key.protection}")
    }

    @Test
    fun destroyRemovesTheKey() {
        store.create()
        store.destroy()
        assertNull(store.load())
    }
}
