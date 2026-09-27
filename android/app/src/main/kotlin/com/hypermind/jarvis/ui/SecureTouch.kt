package com.hypermind.jarvis.ui

import android.view.View

/**
 * Tapjacking protection for the screen where actions are approved: touches
 * that arrive while another app's window obscures this one are dropped
 * (`filterTouchesWhenObscured`). JARVIS's own overlay steps aside while this
 * screen is in front, so it never obscures it.
 */
object SecureTouch {
    fun protect(view: View?) {
        view?.filterTouchesWhenObscured = true
    }
}
