package com.hypermind.jarvis.presentation.character

/**
 * The character's presentation states (OD-CHAR-1/4; mirrors
 * `shared/schemas/presentation.py` `Expression`), in the owner's order —
 * which is also the tie-break order ([ActivityPolicy]). [DEFAULT] is the
 * initial and fallback state.
 *
 * An expression is how the character *looks*: it presents JARVIS's
 * operational activity as the server's activity scorer reads it (OD-CHAR-4) —
 * HAPPY for frequent, productive work, SATISFIED for steady operation, SLEEPY
 * for sustained inactivity, ANGRY for overload (never anger at the user),
 * SURPRISED for an unexpected event, SAD for failed work. It is not a feeling,
 * not a measurement of the user, and not the account of what JARVIS is doing:
 * that comes only from [com.hypermind.jarvis.presentation.PresentationState]
 * and is shown by the status line. Nothing that authorizes, confirms or
 * executes reads it.
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
