package com.hypermind.jarvis.presentation.character

/**
 * The character's presentation states (OD-CHAR-1/2; mirrors
 * `shared/schemas/presentation.py` `Expression`), in the owner's order —
 * which is also the selection tie-break order ([StateSelector]). [DEFAULT] is
 * the initial and fallback state.
 *
 * An expression is how the character *looks*, nothing else. It is not an
 * agent mood, not a task or connection status and not a result: those come
 * only from [com.hypermind.jarvis.presentation.PresentationState] and are
 * shown by the status line. An expression is selected from the current
 * interaction and verified application events (OD-CHAR-2), never from that
 * state, and nothing that authorizes, confirms or executes reads it.
 */
enum class Expression(
    val wire: String,
) {
    HAPPY("happy"),
    SATISFIED("satisfied"),
    DEFAULT("default"),
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
