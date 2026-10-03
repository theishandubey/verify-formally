---- MODULE RetryGuard ----
EXTENDS Naturals

CONSTANTS MaxErrors, MaxTimeouts, Fixed, MutantNoIncrement, MutantAttemptsStrict

VARIABLES errors, timeouts, attempts, status

Vars == <<errors, timeouts, attempts, status>>

MaxAttempts == MaxErrors + MaxTimeouts

TypeOK ==
  /\ errors \in 0..MaxErrors
  /\ timeouts \in 0..MaxTimeouts
  /\ attempts \in Nat
  /\ status \in {"running", "gave_up", "succeeded"}

Init ==
  /\ errors = 0
  /\ timeouts = 0
  /\ attempts = 0
  /\ status = "running"

AttemptsGuard(a) == IF MutantAttemptsStrict THEN a > MaxAttempts ELSE a >= MaxAttempts

GiveUpCond(e, t, a) ==
  \/ e >= MaxErrors
  \/ t >= MaxTimeouts
  \/ (Fixed /\ AttemptsGuard(a))

NextAttempts(a) == IF MutantNoIncrement THEN a ELSE a + 1

SuccessStep ==
  /\ status = "running"
  /\ attempts' = NextAttempts(attempts)
  /\ errors' = errors
  /\ timeouts' = timeouts
  /\ status' = "succeeded"

TransientStep ==
  /\ status = "running"
  /\ LET e == errors + 1
         t == 0
         a == NextAttempts(attempts)
     IN /\ errors' = e
        /\ timeouts' = t
        /\ attempts' = a
        /\ status' = IF GiveUpCond(e, t, a) THEN "gave_up" ELSE "running"

TimeoutStep ==
  /\ status = "running"
  /\ LET e == 0
         t == timeouts + 1
         a == NextAttempts(attempts)
     IN /\ errors' = e
        /\ timeouts' = t
        /\ attempts' = a
        /\ status' = IF GiveUpCond(e, t, a) THEN "gave_up" ELSE "running"

Step == SuccessStep \/ TransientStep \/ TimeoutStep

Done ==
  /\ status # "running"
  /\ UNCHANGED Vars

Next == Step \/ Done

Spec == Init /\ [][Next]_Vars /\ WF_Vars(Step)

BoundedAttempts == attempts <= MaxAttempts

Termination == <>(status # "running")

SanityReachesGaveUp == status # "gave_up"

Bound == 10
StateConstraint == attempts <= Bound
ViewNoAttempts == <<errors, timeouts, status>>

====
