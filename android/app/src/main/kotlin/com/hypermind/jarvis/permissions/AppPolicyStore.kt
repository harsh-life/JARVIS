package com.hypermind.jarvis.permissions

import android.content.SharedPreferences
import com.hypermind.jarvis.contract.AppPolicy
import com.hypermind.jarvis.contract.ContractJson
import kotlinx.serialization.SerializationException

/**
 * The server's sensitive-app classification, cached (docs/23 §5.2,
 * `GET /devices/app-policy`). Used only to refuse early — UI control only in
 * classified apps, screenshots only of non-sensitive ones — and to show the
 * user which apps can be controlled at all. With nothing cached the device is
 * restrictive: `null` is treated as "nothing classified".
 */
class AppPolicyStore(
    private val prefs: SharedPreferences,
) {
    @Synchronized
    fun current(): AppPolicy? =
        prefs.getString(KEY, null)?.let {
            try {
                ContractJson.decodeFromString(AppPolicy.serializer(), it)
            } catch (ignored: SerializationException) {
                null
            } catch (ignored: IllegalArgumentException) {
                null
            }
        }

    @Synchronized
    fun update(policy: AppPolicy) {
        prefs.edit().putString(KEY, ContractJson.encodeToString(AppPolicy.serializer(), policy)).commit()
    }

    @Synchronized
    fun wipe() {
        prefs.edit().remove(KEY).commit()
    }

    private companion object {
        const val KEY = "app_policy"
    }
}
