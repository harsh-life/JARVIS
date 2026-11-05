package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression

/**
 * [CharacterAnimationPlayer] over a [RigRuntime], following the rig's [RigContract]:
 *
 * - **Change of state** A → B: `trans_A_to_B` if the contract has it (pair mix), else `to_B`
 *   (entry mix), then `idle_B` queued. During a transition or long idle the change uses the
 *   interrupt mix instead; `set` drops what was queued, so the old idle never plays after it.
 * - **Same state again:** nothing (no restart).
 * - **Long idle:** `long_idle_<shown>` once, `idle_<shown>` queued after it.
 * - **Entrance / exit:** track 1, layered over track 0; the entrance's track is cleared when it
 *   completes, the exit holds its last frame and marks the character [hidden].
 * - **Reduced motion:** straight to the idle, no entrance, and time never advances.
 * - **Safety net:** a one-shot that completes with nothing after it falls back to the idle, so a
 *   valid state never leaves an old animation playing.
 */
class RigCharacterPlayer private constructor(
    private val runtime: RigRuntime,
    private val contract: RigContract,
    private val reducedMotion: Boolean,
    private val index: Map<String, Int>,
) : CharacterAnimationPlayer {
    override var expression: Expression = Expression.DEFAULT
        private set
    override var paused = false
        private set
    override var hidden = false
        private set
    override var released = false
        private set

    private var started = false
    private var skipNextTime = false
    private val events = IntArray(RigEvents.BUFFER)

    /** Started and not released: the only state in which the player touches the runtime. */
    private val live: Boolean get() = started && !released

    /** Live, playing and allowed to move: when a long idle may start. */
    private val moving: Boolean get() = live && !paused && !reducedMotion

    private fun idOf(name: String): Int = index.getValue(name)

    private fun idleOf(e: Expression): Int = idOf(contract.idle.getValue(e))

    private val idling: Boolean get() = runtime.current(TRACK_STATE) == idleOf(expression)

    override fun start(
        initial: Expression,
        entrance: Boolean,
    ) {
        if (released) return
        expression = initial
        started = true
        hidden = false
        runtime.setAnimation(TRACK_STATE, idleOf(initial), loop = true, mix = 0f)
        if (entrance &&
            !reducedMotion
        ) {
            runtime.setAnimation(TRACK_OVERLAY, idOf(contract.enter), loop = false, mix = 0f)
        }
    }

    override fun show(expression: Expression) {
        if (released) return
        if (!started) return start(expression, entrance = false)
        if (expression == this.expression) return
        val from = this.expression
        this.expression = expression
        if (reducedMotion) {
            runtime.setAnimation(TRACK_STATE, idleOf(expression), loop = true, mix = 0f)
            return
        }
        val pair = contract.pairs[from to expression]
        val mix =
            when {
                !idlingAs(from) -> contract.mix.interrupt
                pair != null -> contract.mix.intoPair
                else -> contract.mix.intoEntry
            }
        runtime.setAnimation(TRACK_STATE, idOf(pair ?: contract.entry.getValue(expression)), loop = false, mix = mix)
        runtime.addAnimation(TRACK_STATE, idleOf(expression), loop = true, mix = contract.mix.toIdle)
    }

    private fun idlingAs(e: Expression): Boolean = runtime.current(TRACK_STATE) == idleOf(e)

    override fun triggerLongIdle(): Boolean {
        if (!moving || !idling) return false
        runtime.setAnimation(
            TRACK_STATE,
            idOf(contract.longIdle.getValue(expression)),
            loop = false,
            mix = contract.mix.intoLongIdle,
        )
        runtime.addAnimation(TRACK_STATE, idleOf(expression), loop = true, mix = contract.mix.longIdleToIdle)
        return true
    }

    override fun returnToIdle() {
        if (released || !started || idling) return
        runtime.setAnimation(
            TRACK_STATE,
            idleOf(expression),
            loop = true,
            mix = if (reducedMotion) 0f else contract.mix.interrupt,
        )
    }

    override fun enter() {
        if (released) return
        hidden = false
        if (!reducedMotion) {
            runtime.setAnimation(
                TRACK_OVERLAY,
                idOf(contract.enter),
                loop = false,
                mix = 0f,
            )
        } else {
            runtime.clearTrack(TRACK_OVERLAY)
        }
    }

    override fun exit() {
        if (released) return
        if (reducedMotion) {
            hidden = true
            return
        }
        runtime.setAnimation(TRACK_OVERLAY, idOf(contract.exit), loop = false, mix = 0f)
    }

    override fun pause() {
        if (!released) paused = true
    }

    override fun resume() {
        if (released || !paused) return
        paused = false
        // The time spent paused is not played: the first frame after it advances nothing.
        skipNextTime = true
    }

    override fun advance(seconds: Float) {
        if (released || paused || !started) return
        val dt =
            when {
                reducedMotion || skipNextTime -> 0f
                seconds.isFinite() -> seconds.coerceIn(0f, MAX_STEP)
                else -> 0f
            }
        skipNextTime = false
        val n = runtime.update(dt, events)
        for (k in 0 until n.coerceAtLeast(0)) handle(events[k * 3], events[k * 3 + 1], events[k * 3 + 2])
    }

    private fun handle(
        kind: Int,
        track: Int,
        animation: Int,
    ) {
        if (kind != RigEvents.COMPLETE) return
        when {
            track == TRACK_OVERLAY && animation == idOf(contract.enter) -> runtime.clearTrack(TRACK_OVERLAY)
            track == TRACK_OVERLAY && animation == idOf(contract.exit) -> hidden = true
            track == TRACK_STATE && animation != idleOf(expression) && runtime.current(TRACK_STATE) == animation ->
                runtime.setAnimation(TRACK_STATE, idleOf(expression), loop = true, mix = contract.mix.toIdle)
        }
    }

    override fun build(geometry: RigGeometry): Boolean = !released && started && runtime.build(geometry)

    override fun close() {
        if (released) return
        released = true
        runtime.close()
    }

    companion object {
        /** Track 0: the state (idles, transitions, long idles). Track 1: entrance and exit. */
        const val TRACK_STATE = 0
        const val TRACK_OVERLAY = 1

        /** The longest step one frame may advance (a stalled frame never jumps the animation). */
        const val MAX_STEP = 0.25f

        /**
         * A player for [runtime], or null if the rig lacks an animation the contract names (then
         * [runtime] is closed, and the screen keeps the static images).
         */
        fun create(
            runtime: RigRuntime,
            contract: RigContract,
            reducedMotion: Boolean = false,
        ): RigCharacterPlayer? {
            val index = contract.animationNames.associateWith { runtime.animationIndex(it) }
            if (index.values.any { it < 0 }) {
                runtime.close()
                return null
            }
            return RigCharacterPlayer(runtime, contract, reducedMotion, index)
        }
    }
}
