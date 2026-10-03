---- MODULE Liveness ----
EXTENDS Naturals
VARIABLE x

Init == x = 0
Flip == x' = 1 - x

Next == Flip

Spec == Init /\ [][Next]_x /\ WF_x(Next)

Done == x = 2
Termination == <>Done

====
