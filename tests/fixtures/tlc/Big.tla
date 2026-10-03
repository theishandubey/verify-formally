---- MODULE Big ----
EXTENDS Naturals
VARIABLE x, y
Init == x = 0 /\ y = 0
Next == x' \in 0..100000 /\ y' \in 0..100000
====
