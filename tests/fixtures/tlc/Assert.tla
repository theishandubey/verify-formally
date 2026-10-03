---- MODULE Assert ----
EXTENDS Naturals, TLC
VARIABLE x
Init == x = 0
Next == /\ Assert(x < 3, "x too big")
        /\ x' = x + 1
====
