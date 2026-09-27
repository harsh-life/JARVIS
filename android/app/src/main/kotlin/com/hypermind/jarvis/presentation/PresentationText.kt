package com.hypermind.jarvis.presentation

import com.hypermind.jarvis.contract.RiskCategory

/**
 * Plain-language wording for a [PresentationState] — one table for every
 * surface (task panel, overlay, notification). Built only from the state's
 * signals, so it can never contain the user's words, a model's prose, screen
 * content, an id or a token.
 *
 * Two rules the wording keeps (docs/23 §5.3): a waiting **task** is described
 * as paused, never as having anything lined up to run; and continuing is
 * always described as the server checking again first.
 */
object PresentationText {
    data class Wording(
        val headline: String,
        val detail: String? = null,
        /** Where the user can go to fix it. Navigation only — never an approval or a grant. */
        val action: UserAction? = null,
    )

    enum class UserAction {
        OPEN_ACCESSIBILITY_SETTINGS,
        OPEN_NOTIFICATION_ACCESS_SETTINGS,
        OPEN_SHIZUKU,
        CONNECT,
        SIGN_IN_AGAIN,
        UPDATE_APP,
        OPEN_APP_TO_APPROVE,
        TRY_AGAIN,
    }

    fun of(state: PresentationState): Wording =
        when (state.taskStatus) {
            TaskPhase.IDLE -> Wording("Ready")
            TaskPhase.LISTENING -> Wording("Listening")
            TaskPhase.SUBMITTING -> Wording("Sending your request")
            TaskPhase.THINKING -> Wording("Working on it")
            TaskPhase.EXECUTING -> Wording(activity(state.deviceContext.activity))
            TaskPhase.WAITING_FOR_CONFIRMATION -> confirmation(state)
            TaskPhase.WAITING_FOR_PLATFORM -> waiting(state.waitingFor)
            TaskPhase.COMPLETED -> Wording("Done")
            TaskPhase.CANCELLED -> Wording("Cancelled", "Nothing further will run for this task.")
            TaskPhase.FAILED, TaskPhase.BLOCKED -> error(state.error)
            TaskPhase.OFFLINE ->
                state.error?.let(::error)
                    ?: Wording("Not connected", "Connect to your JARVIS server to use this phone.", UserAction.CONNECT)
            TaskPhase.RECONNECTING ->
                if (state.error?.kind == ErrorKind.AUTH_EXPIRED) {
                    Wording("Signing in again", "Your session expired; this phone is proving itself to the server.")
                } else {
                    Wording("Reconnecting", "This phone is getting back in touch with your server.")
                }
        }

    private fun activity(activity: DeviceActivity?): String =
        when (activity) {
            DeviceActivity.READING_SCREEN -> "Reading the screen"
            DeviceActivity.READING_CONTROLS -> "Reading app controls"
            DeviceActivity.USING_OCR -> "Reading text on screen (OCR)"
            DeviceActivity.VISUAL_FALLBACK -> "Using the visual fallback"
            DeviceActivity.READING_NOTIFICATIONS -> "Reading notifications"
            DeviceActivity.READING_DEVICE_STATE -> "Checking the phone's state"
            DeviceActivity.ACTING_IN_APP -> "Acting in the app"
            DeviceActivity.PRIVILEGED_ACTION -> "Running a privileged action"
            DeviceActivity.WORKING, null -> "Working on this phone"
        }

    private fun confirmation(state: PresentationState): Wording {
        val detail =
            when (state.riskTierPending) {
                RiskCategory.HIGH_IRREVERSIBLE -> "This cannot be undone. Approving asks you to verify it is you."
                RiskCategory.CONSEQUENTIAL -> "This acts on your behalf. Review exactly what will happen."
                else -> "Review exactly what will happen."
            }
        val stepUp =
            when (state.error?.kind) {
                ErrorKind.STEP_UP_CANCELLED -> "Not verified — nothing was approved; the action is still waiting."
                ErrorKind.STEP_UP_UNAVAILABLE ->
                    "This phone cannot verify you (set a screen lock, then sign in again). Nothing was approved."
                ErrorKind.STEP_UP_FAILED -> "Verification failed. Nothing was approved."
                else -> null
            }
        return Wording(
            "Needs your approval",
            listOfNotNull(stepUp, detail).joinToString(" "),
            UserAction.OPEN_APP_TO_APPROVE,
        )
    }

    /** What is missing, why it is needed, that the task is paused, and what to do. */
    fun waiting(waiting: WaitingFor?): Wording {
        val dependency = waiting?.dependency ?: WaitDependency.UNKNOWN
        val (name, why, action) = DEPENDENCIES.getValue(dependency)
        val now =
            if (waiting?.availableNow == true) {
                " It looks available now — your server will continue shortly."
            } else {
                ""
            }
        val detail =
            "$why The task is paused until then; nothing runs in the meantime. " +
                "When it is back, your server checks everything again before continuing.$now"
        return Wording("Waiting for $name", detail, action)
    }

    private val DEPENDENCIES: Map<WaitDependency, Triple<String, String, UserAction?>> =
        mapOf(
            WaitDependency.SHIZUKU to
                Triple(
                    "Shizuku",
                    "This step needs Shizuku (for example, to stop an app). Start Shizuku and allow JARVIS in it.",
                    UserAction.OPEN_SHIZUKU,
                ),
            WaitDependency.ACCESSIBILITY_SERVICE to
                Triple(
                    "Accessibility access",
                    "This step reads or acts on the screen, which needs JARVIS's Accessibility service turned on.",
                    UserAction.OPEN_ACCESSIBILITY_SETTINGS,
                ),
            WaitDependency.NOTIFICATION_ACCESS to
                Triple(
                    "notification access",
                    "This step reads notifications, which needs notification access for JARVIS.",
                    UserAction.OPEN_NOTIFICATION_ACCESS_SETTINGS,
                ),
            WaitDependency.SCREEN_CAPTURE to
                Triple(
                    "screen capture",
                    "This step takes a screenshot, which needs the Accessibility service (Android 11 or later).",
                    UserAction.OPEN_ACCESSIBILITY_SETTINGS,
                ),
            WaitDependency.OCR to Triple("text recognition", "This step reads text in an image on the phone.", null),
            WaitDependency.DEVICE_CHANNEL to
                Triple(
                    "this phone to reconnect",
                    "This step runs on this phone, which was not connected to your server.",
                    UserAction.CONNECT,
                ),
            WaitDependency.UNKNOWN to Triple("a phone setting", "This step needs something on the phone.", null),
        )

    fun error(error: PresentationError?): Wording =
        when (error?.kind) {
            ErrorKind.OFFLINE -> Wording("Not connected", null, UserAction.CONNECT)
            ErrorKind.SERVER_UNREACHABLE ->
                Wording(
                    "Could not reach your server",
                    "Your request may or may not have arrived. Trying again is safe — it is never run twice.",
                    UserAction.TRY_AGAIN,
                )
            ErrorKind.SERVER_UNAVAILABLE -> Wording("Your server is unavailable right now", "Try again in a moment.")
            ErrorKind.AUTH_EXPIRED -> Wording("Signing in again", "Your session expired.")
            ErrorKind.REVOKED ->
                Wording("This phone was removed", "Sign in again to use it with JARVIS.", UserAction.SIGN_IN_AGAIN)
            ErrorKind.UPDATE_REQUIRED ->
                Wording("Update the app", "It no longer matches your server.", UserAction.UPDATE_APP)
            ErrorKind.CHANNEL_DISABLED -> Wording("Device actions are off on your server")
            ErrorKind.REFUSED -> Wording("Not allowed", "Your server refused this request. Nothing was done.")
            ErrorKind.STOPPED_BY_OPERATOR -> Wording("Stopped", "The task was stopped for safety and will not resume.")
            ErrorKind.LIMIT_REACHED -> Wording("Limit reached", "A usage limit stopped the task. Try again later.")
            ErrorKind.CONFIRMATION_EXPIRED ->
                Wording("The approval expired", "Nothing was done. Ask again to get a fresh approval.")
            ErrorKind.STEP_UP_CANCELLED, ErrorKind.STEP_UP_UNAVAILABLE, ErrorKind.STEP_UP_FAILED ->
                Wording("Not verified", "Nothing was approved.")
            ErrorKind.DEVICE_UNAVAILABLE -> Wording("This phone was not reachable", null, UserAction.CONNECT)
            ErrorKind.PLATFORM_UNAVAILABLE ->
                Wording("A phone setting was still off", "The task stopped waiting for it. Nothing was done.")
            ErrorKind.OPERATION_EXPIRED -> Wording("Too late to act", "The step expired before it could run.")
            ErrorKind.TASK_NOT_FOUND -> Wording("That task is gone")
            ErrorKind.INVALID_RESPONSE ->
                Wording(
                    "Unexpected answer from your server",
                    "Nothing was done on this phone.",
                )
            ErrorKind.TASK_FAILED, null -> Wording("The task did not finish")
        }
}
