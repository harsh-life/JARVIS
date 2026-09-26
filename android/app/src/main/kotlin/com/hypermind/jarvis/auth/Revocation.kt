package com.hypermind.jarvis.auth

/**
 * Everything a revoked (or signed-out) device forgets (docs/23 §4
 * "Revocation"): the credential, the enrollment, the in-memory token, and
 * every piece of local grant state registered here. After this the device
 * holds nothing that could authorize or execute anything, and a new Google
 * login and enrollment is the only way back.
 */
class Revocation(
    private val keys: DeviceKeyStore,
    private val store: EnrollmentStore,
    private val sessions: SessionManager,
) {
    private val localState = mutableListOf<() -> Unit>()

    /** Local grant state (the per-app grid, docs/23 §5.2) registers its wipe here. */
    @Synchronized
    fun onWipe(wipe: () -> Unit) {
        localState += wipe
    }

    @Synchronized
    fun wipe() {
        sessions.forget()
        keys.destroy()
        store.clearEnrollment()
        localState.forEach { it() }
    }
}
