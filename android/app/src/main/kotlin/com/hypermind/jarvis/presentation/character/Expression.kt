package com.hypermind.jarvis.presentation.character

/**
 * The character's presentation states (OD-CHAR-1; mirrors
 * `shared/schemas/presentation.py` `Expression`). [DEFAULT] is the initial
 * and fallback state.
 *
 * An expression is how the character *looks*, nothing else. It is not an
 * agent mood, not a task or connection status and not a result: those come
 * only from [com.hypermind.jarvis.presentation.PresentationState] and are
 * shown by the status line. Nothing here is derived from that state, and
 * nothing that authorizes, confirms or executes reads an expression.
 */
enum class Expression(
    val wire: String,
) {
    DEFAULT("default"),
    SATISFIED("satisfied"),
    HAPPY("happy"),
    SLEEPY("sleepy"),
    SURPRISED("surprised"),
    ANGRY("angry"),
    SAD("sad"),
    ;

    companion object {
        /** A supplied state, validated: anything unknown (or absent) is [DEFAULT], never an error. */
        fun parse(wire: String?): Expression = entries.firstOrNull { it.wire == wire } ?: DEFAULT
    }
}
