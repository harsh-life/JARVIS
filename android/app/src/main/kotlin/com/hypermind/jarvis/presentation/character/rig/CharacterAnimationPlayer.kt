package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression
import com.hypermind.jarvis.presentation.character.IntensityPolicy

/**
 * Plays the animated character: the replaceable boundary between the character screen and an
 * animation runtime. It is told which [Expression] to show and at which intensity (1–10) — both
 * decided by `CharacterController`, through the screen — and only animates them: it never chooses
 * a state or an intensity, never sees what was said, infers nothing, and nothing it does reaches
 * tasks, status, approvals or execution.
 *
 * Confined to one thread (the main thread). After [close] every call is a no-op.
 */
interface CharacterAnimationPlayer : AutoCloseable {
    /** The state being shown (or transitioned into). */
    val expression: Expression

    /** How strongly [expression] is presented (1–10; 5 and 6 are the canonical animation). */
    val intensity: Int

    /** True while [pause]d: time stands still. */
    val paused: Boolean

    /** True once the exit has finished (the character has disappeared). */
    val hidden: Boolean

    /** True after [close]. */
    val released: Boolean

    /** Starts on [initial]'s idle at [intensity], with the entrance if [entrance]. */
    fun start(
        initial: Expression,
        intensity: Int = IntensityPolicy.NEUTRAL,
        entrance: Boolean,
    )

    /**
     * Shows [expression] at [intensity]: its transition, then its idle. The same expression again
     * restarts nothing (a new intensity only retargets how strongly it is presented, ramping); a
     * new one interrupts whatever plays (a transition, a long idle). The same pair again is a no-op.
     */
    fun show(
        expression: Expression,
        intensity: Int = IntensityPolicy.NEUTRAL,
    )

    /**
     * Plays the shown state's long-idle action once, then its idle again. Refused (false) during a
     * transition, while paused, in reduced motion or if one is already playing. The caller owns the
     * timer (LongIdleSchedule); the player has none.
     */
    fun triggerLongIdle(): Boolean

    /** Back to the shown state's idle now, cutting short whatever plays. */
    fun returnToIdle()

    fun enter()

    fun exit()

    fun pause()

    fun resume()

    /** Advances time by [seconds] (one display frame). */
    fun advance(seconds: Float)

    /** Writes the current frame's triangles into [geometry]; false if there is nothing to draw. */
    fun build(geometry: RigGeometry): Boolean
}
