package com.hypermind.jarvis.presentation.character

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * How this client presents the character — independent of the profile's
 * state → image mapping (mirrors `shared/schemas/presentation.py`
 * `CharacterPresentationSettings`). Built-in defaults today; a later
 * configuration source (the configuration website) supplies a new value.
 */
data class CharacterSettings(
    val visible: Boolean = true,
    val profileId: String = CharacterProfile.BUILT_IN.profileId,
    val transition: Transition = Transition.CROSSFADE,
) {
    enum class Transition { CROSSFADE, NONE }
}

/**
 * The character's one input (OD-CHAR-1): which [Expression] to show, in
 * memory only. It starts at [Expression.DEFAULT] and changes only when
 * something calls [select] — today the development preview; later a
 * classifier or orchestrator, which hands its answer over as a wire value and
 * gets it validated here. It is never derived from task, connection or error
 * state, never persisted and never sent anywhere, and nothing that
 * authorizes, confirms or executes reads it.
 */
class CharacterPresentation(
    val profile: CharacterProfile = CharacterProfile.BUILT_IN,
    settings: CharacterSettings = CharacterSettings(),
) {
    private val current = MutableStateFlow(Expression.DEFAULT)
    private val presentation = MutableStateFlow(settings)

    val expression: StateFlow<Expression> = current.asStateFlow()
    val settings: StateFlow<CharacterSettings> = presentation.asStateFlow()

    fun select(expression: Expression) {
        current.value = expression
    }

    /** A supplied state by its wire name; anything unknown shows [Expression.DEFAULT]. */
    fun select(wire: String?) = select(Expression.parse(wire))

    fun reset() = select(Expression.DEFAULT)

    fun configure(settings: CharacterSettings) {
        presentation.value = settings
    }
}
