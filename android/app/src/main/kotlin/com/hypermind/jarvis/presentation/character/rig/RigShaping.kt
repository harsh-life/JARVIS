package com.hypermind.jarvis.presentation.character.rig

import com.hypermind.jarvis.presentation.character.Expression

/** Applies an intensity profile to a loaded rig (docs/CHARACTER_RIG.md §5). */
object RigShaping {
    /**
     * Configures [runtime]'s intensity layers from [profile] and [contract], once, while loading:
     * each expression's regular idle, the reference expression's idle, the range of every
     * track-0 animation (not the entrance or exit), and per-bone weights from the profile's held
     * bones. Returns [profile] if the runtime accepted it, else null — a bone the rig does not
     * have, an animation it lacks, or a refusal — and the character then plays canonically (it
     * never falls back to the static images over the optional profile).
     */
    fun configure(
        runtime: RigRuntime,
        contract: RigContract,
        profile: RigIntensityProfile,
    ): RigIntensityProfile? {
        val bones = runtime.info.boneCount
        if (bones <= 0) return null
        val idleNames = Expression.entries.map { contract.idle.getValue(it) }
        val idles = idleNames.map { runtime.animationIndex(it) }
        val rangeNames = contract.idle.values + contract.longIdle.values + contract.entry.values + contract.pairs.values
        val range = rangeNames.toSet().map { runtime.animationIndex(it) }
        val reference = runtime.animationIndex(contract.idle.getValue(profile.reference))
        if (idles.any { it < 0 } || range.any { it < 0 } || reference < 0) return null
        val postureWeights = weights(runtime, bones, profile.postureHolds) ?: return null
        val motionWeights = weights(runtime, bones, profile.motionHolds) ?: return null
        val ok =
            runtime.configureShape(
                reference = reference,
                idles = idles.toIntArray(),
                range = range.toIntArray(),
                postureWeights = postureWeights,
                motionWeights = motionWeights,
                postureRampSeconds = profile.postureRampSeconds,
                motionRampSeconds = profile.motionRampSeconds,
            )
        return profile.takeIf { ok }
    }

    /** One weight per bone for each expression (in [Expression] order): 0 for a held bone, else 1. */
    private fun weights(
        runtime: RigRuntime,
        bones: Int,
        holds: Map<Expression, Set<String>>,
    ): FloatArray? {
        val out = FloatArray(Expression.entries.size * bones) { 1f }
        for ((i, e) in Expression.entries.withIndex()) {
            for (name in holds.getValue(e)) {
                val b = runtime.boneIndex(name)
                if (b !in 0 until bones) return null
                out[i * bones + b] = 0f
            }
        }
        return out
    }
}
