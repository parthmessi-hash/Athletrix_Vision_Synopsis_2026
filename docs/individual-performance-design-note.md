# Proposed design note: individual performance review (NOT implemented)

Status: a record of the team's current discussion (2026-10-29). **Nothing in this note is implemented.** No score,
weighting, tactical judgment, pass-quality model, fatigue classifier or substitution logic exists in Athletrix, and
none should be inferred from the camera adapter prototype.

## What the team has discussed

A future review of one player's performance might look at several separate dimensions, kept apart rather than merged:

1. **Execution**: whether an action was carried out as intended (for example a pass reaching its target).
2. **Decision quality**: whether the chosen action was a good choice given the situation and the options available.
3. **Movement and positioning**: where the player was and how they moved relative to teammates, opponents and the
   ball, and relative to what the role asked for.
4. **Adherence to explicitly entered coach instructions**: only instructions a coach has written down in the
   system for that player, role and situation; never instructions the system assumes.
5. **A shared rubric for comparable roles and situations**: the same written criteria applied to players in the same
   role and comparable situations, so that comparisons are like for like.
6. **Personal performance history as separate context**: a player's own earlier reviews shown beside the current one,
   not blended into it.
7. **Physiological response compared with the individual's own baseline**: descriptive context (for example heart rate
   during and after an effort compared with that person's usual response), never a diagnosis or a readiness verdict.
8. **"Unable to assess" when evidence is insufficient**: every dimension must be able to say so, and missing data must
   never be scored as poor performance or filled in.

## What does not exist

- No agreed weights between these dimensions and no validated composite score. Any combination would need a stated
  method, expert agreement and validation before use.
- No tactical model, so the current prototype cannot know whether a pass was the right decision.
- No detection of people, the ball or events. The camera adapter prototype uses **synthetic markers** found by colour;
  it must eventually be replaced or supplemented by validated person detection, multi-object tracking, athlete
  assignment (identity) and, where needed, ball and event detection, each evaluated on real footage before any of the
  dimensions above could be measured.
- No fatigue classification, injury prediction or substitution decision; these remain out of scope.

## Preconditions before any implementation

Written rubrics per sport and role, defined evidence for each dimension, an explicit "unable to assess" path, review
by coaches and qualified adults, validation on real data collected under an approved procedure, and a plan for how
players see and can question their own reviews.
