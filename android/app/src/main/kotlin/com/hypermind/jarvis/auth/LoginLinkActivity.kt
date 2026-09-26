package com.hypermind.jarvis.auth

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import com.hypermind.jarvis.JarvisApplication
import com.hypermind.jarvis.MainActivity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException

/**
 * The verified App Link target (docs/23 §3). Hands the link to
 * [LoginCoordinator], which accepts it only for this app's own pending login,
 * then returns to the main screen with the outcome. Never displays or logs the
 * link: its fragment carries a bootstrap token.
 */
class LoginLinkActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val link = intent?.data
        val graph = (application as JarvisApplication).graph
        lifecycleScope.launch {
            val message =
                if (link == null) {
                    "Sign-in link was empty."
                } else {
                    withContext(Dispatchers.IO) {
                        try {
                            when (val outcome = graph.login.complete(link)) {
                                is LoginOutcome.Enrolled -> "This phone is now enrolled."
                                is LoginOutcome.Rejected -> rejection(outcome.reason)
                            }
                        } catch (e: ApiException) {
                            "The server refused the sign-in (${e.code}). Start again."
                        } catch (ignored: IOException) {
                            "Could not reach the server. Start sign-in again."
                        } catch (ignored: EnrollmentLost) {
                            "Enrollment did not complete. Start sign-in again."
                        }
                    }
                }
            graph.forgetKey()
            startActivity(
                Intent(this@LoginLinkActivity, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(MainActivity.EXTRA_NOTICE, message),
            )
            finish()
        }
    }

    private fun rejection(reason: LoginRejection): String =
        when (reason) {
            LoginRejection.NO_PENDING_LOGIN -> "This sign-in was not started from this app, so it was ignored."
            LoginRejection.EXPIRED -> "That sign-in took too long. Start again."
            LoginRejection.STATE_MISMATCH -> "That sign-in belongs to a different request, so it was ignored."
            LoginRejection.MALFORMED_LINK -> "The sign-in link was malformed."
        }
}
